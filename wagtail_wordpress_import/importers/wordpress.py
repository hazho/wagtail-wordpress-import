import json
from datetime import datetime
from xml.dom import pulldom

from django.apps import apps
from django.utils.text import slugify
from django.utils.timezone import make_aware
from wagtail.core import blocks
from wagtail.core.models import Page
from wagtail_wordpress_import.bleach import bleach_clean, fix_styles
from wagtail_wordpress_import.block_builder import BlockBuilder
from wagtail_wordpress_import.functions import linebreaks_wp, node_to_dict
from wagtail_wordpress_import.importers import wordpress_mapping


class WordpressImporter:
    def __init__(self, xml_file_path):
        self.xml_file = xml_file_path
        self.mapping = wordpress_mapping.mapping
        self.mapping_item = self.mapping.get("item")
        self.mapping_valid_date = self.mapping.get("validate_date")
        self.mapping_valid_slug = self.mapping.get("validate_slug")
        self.mapping_stream_fields = self.mapping.get("stream_fields")
        self.mapping_item_inverse = self.map_item_inverse()
        self.log_processed = 0
        self.log_imported = 0
        self.log_skipped = 0
        self.logged_items = []

    def run(self, *args, **kwargs):
        xml_doc = pulldom.parse(self.xml_file)

        try:
            self.page_model_instance = apps.get_model(
                kwargs["app_for_pages"], kwargs["model_for_pages"]
            )
        except LookupError:
            print(
                f"The app `{kwargs['app_for_pages']}` and/or page model `{kwargs['model_for_pages']}` cannot be found!"
            )
            print(
                "Check the command line options -a and -m match an existing Wagtail app and Wagtail page model"
            )
            exit()

        try:
            self.parent_page_obj = Page.objects.get(pk=kwargs["parent_id"])
        except Page.DoesNotExist:
            print(f"A page with id {kwargs['parent_id']} does not exist")
            exit()

        for event, node in xml_doc:
            # each node represents a tag in the xml
            # event is true for the start element
            if event == pulldom.START_ELEMENT and node.tagName == "item":
                xml_doc.expandNode(node)
                item = node_to_dict(node)
                self.log_processed += 1
                if (
                    item.get("wp:post_type") in kwargs["page_types"]
                    and item.get("wp:status") in kwargs["page_statuses"]
                ):

                    # dates_valid and slugs_valid might be useful for
                    # loging detail
                    item_dict, dates_valid, slugs_valid = self.get_values(item)

                    page_exists = self.page_model_instance.objects.filter(
                        wp_post_id=item.get("wp:post_id")
                    ).first()

                    if page_exists:
                        self.update_page(page_exists, item_dict, item.get("wp:status"))
                        item["log"] = {
                            "result": "updated",
                            "reason": "existed",
                            "datecheck": dates_valid,
                            "slugcheck": slugs_valid,
                        }
                    else:
                        self.create_page(item_dict, item.get("wp:status"))
                        item["log"] = {
                            "result": "created",
                            "reason": "new",
                            "datecheck": dates_valid,
                            "slugcheck": slugs_valid,
                        }
                    self.log_imported += 1
                else:
                    item["log"] = {
                        "result": "skipped",
                        "reason": "no title or status match",
                        "datecheck": "",
                        "slugcheck": "",
                    }
                    self.log_skipped += 1

                print(item.get("title"), item.get("log")["result"])
                self.logged_items.append(item)

        return (
            self.log_imported,
            self.log_skipped,
            self.log_processed,
            self.logged_items,
        )

    def analyze_html(self, html_analyzer, *, page_types, page_statuses):
        xml_doc = pulldom.parse(self.xml_file)

        for event, node in xml_doc:
            # each node represents a tag in the xml
            # event is true for the start element
            if event == pulldom.START_ELEMENT and node.tagName == "item":
                xml_doc.expandNode(node)
                item = node_to_dict(node)
                if (
                    item.get("wp:post_type") in page_types
                    and item.get("wp:status") in page_statuses
                ):
                    stream_fields = self.mapping_stream_fields.split(",")

                    for html in stream_fields:
                        value = linebreaks_wp(
                            item.get(self.mapping_item_inverse.get(html))
                        )
                        html_analyzer.analyze(value)

    def create_page(self, values, status):
        obj = self.page_model_instance(**values)

        if status == "draft":
            setattr(obj, "live", False)
        else:
            setattr(obj, "live", True)

        self.parent_page_obj.add_child(instance=obj)

        return obj, "created"

    def update_page(self, page_exists, values, status):
        obj = page_exists

        for key in values.keys():
            setattr(obj, key, values[key])

        obj.save()

        if status == "draft":
            obj.unpublish()

        return obj, "updated"

    def map_item_inverse(self):
        inverse = {}

        for key, value in self.mapping_item.items():
            value = value.split(",")

            if len(value) == 1:
                inverse[value[0]] = key
            else:
                for i in range(len(value)):
                    inverse[value[i]] = key

        return inverse

    def get_values(self, item):
        page_values = {}

        # fields on a page model in wagtail that require a specific input format
        date_valid = []
        date_fields = self.mapping_valid_date.split(",")

        slug_changed = False
        slug_fields = self.mapping_valid_slug.split(",")

        stream_fields = self.mapping_stream_fields.split(",")

        for field, mapped in self.mapping_item_inverse.items():
            page_values[field] = item[mapped]

        # stream fields
        for html in stream_fields:
            sfv, value, blocks = self.parse_stream_fields(
                item.get(self.mapping_item_inverse.get(html))
            )
            page_values[html] = sfv
            page_values["wp_processed_content"] = value
            page_values["wp_block_json"] = json.dumps(blocks, indent=4)

        # dates
        for df in date_fields:
            date = self.parse_date(item.get(self.mapping_item_inverse.get(df)))
            page_values[df] = date[0]
            date_valid.append(date[1])

        # slugs
        for sf in slug_fields:
            slug = self.parse_slug(
                item.get(self.mapping_item_inverse.get(sf)), item.get("title")
            )
            page_values[sf] = slug[0]
            slug_changed = slug[1]

        # if any of the date are not valid then that's important
        date_valid = all(date_valid)

        return page_values, date_valid, slug_changed

    def parse_date(self, value):
        """
        We need a nice date to be able to save the page later. Some dates are not suitable
        date strings in the xml. If thats the case return a specific date so it can be saved
        and return the failure for logging
        """
        valid = True
        if value == "0000-00-00 00:00:00":
            value = "1900-01-01 00:00:00"  # set this date so it can be found in wagtail admin
            valid = False

        date_utc = "T".join(value.split(" "))
        formatted = make_aware(datetime.strptime(date_utc, "%Y-%m-%dT%H:%M:%S"))

        return formatted, valid

    def parse_slug(self, value, title):
        """
        Oddly some page have no slug and some have illegal characters!
        If None make one from title.
        Also pass any slug through slugify to be sure and if it's chnaged make a note
        """
        changed = None

        if not value:
            slug = slugify(title)
            changed = "blank slug"
        else:
            slug = slugify(value)
            changed = "OK"

        # some slugs have illegal characters so will be changed
        if value and slug != value:
            changed = "illegal chars found"

        return slug, changed

    def parse_stream_fields(self, value):
        """
        Here the value is passed through a number of `filters`.
        They will normalize the html and alter inline styles to suit our needs
        Finally the normalized content is passed to the BlockBuilder to create
        the stream fields we need

        Bleach: I'm thinking that bleach clean is removing code that we might want to
        keep or at least take some action on when fixing the styles so running it before
        BlockBuilder
        """
        value = linebreaks_wp(str(value))
        value = fix_styles(str(value))
        value = bleach_clean(str(value))
        blocks = BlockBuilder(value).build()
        return json.dumps(blocks), value, blocks