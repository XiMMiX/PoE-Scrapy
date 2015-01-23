# -*- coding: utf-8 -*-

# Define your item pipelines here
#
# Don't forget to add your pipeline to the ITEM_PIPELINES setting
# See: http://doc.scrapy.org/en/latest/topics/item-pipeline.html

import os, re
from scrapy import signals, log
from scrapy.contrib.exporter import XmlItemExporter, PprintItemExporter
import poe_scrape
import time
from lxml import html
import sys


def _get_category(spider):
    '''List_of_unique_boots -> Boots'''
    path = spider.path
    if not path.startswith("List_of_unique"):
        return "Invalid Category"
    last_underscore = path.rfind("_")
    return path[last_underscore+1:].capitalize()


def _get_words(text):
    return re.findall(r'([a-zA-Z]+)', text)


class DataTransform(object):
    
    def __init__(self, match_rules, processor):
        self.match_rules = match_rules
        self.processor = processor
    
    def is_transformed(self, data):
        return (self.processor.value_separator in data)
    
    def apply_match_rule(self, rule, data):
        return re.sub(rule['match'], 
                      rule['replace'], data, rule.get('options', 0))
        
    def transform(self, data, catgeory):
        match_rules = self.match_rules
        if not isinstance(match_rules, list):
            match_rules = [match_rules]
        for rule in match_rules:
            exclude = rule.get('exclude', None)
            if exclude is not None:
                if isinstance(exclude, list) and catgeory in exclude:
                    log.msg("Excluding rule '%s' for category %s" %  
                            (rule['name'], catgeory), log.DEBUG)
                    continue
                elif isinstance(exclude, str) and re.search(exclude, data):
                    log.msg("Excluding rule '%s' for data %s" %  
                            (rule['name'], data), log.DEBUG)
                    continue
            include = rule.get('include', None)
            if include is not None:
                if isinstance(include, list) and catgeory not in include:
                    log.msg("Skipping rule '%s': category %s not in 'include'" %  
                            (rule['name'], catgeory), log.DEBUG)
                    continue
                elif isinstance(include, str) and re.search(include, data) is None:
                    log.msg("Skipping rule '%s': data %s not matched by 'include'" %  
                            (rule['name'], catgeory), log.DEBUG)
                    continue
            if rule.get('replace', None) is not None:
                data = self.apply_match_rule(rule, data)
        return data


class TextTransform(DataTransform):
    
    match_rules = [
        { # capitalize
            'name': 'Capitalize letter after colon',
            'match': r":(\w)",
            'replace': (lambda match: ':{}'.format(match.group(1).upper())),
            #'exclude': ['Maps']
        },
        { # em dash
            'name': 'Em dash -> dash',
            'match': u"\2013",
            'replace': "-"
        },
        { # To to
            'name': 'To to -> To',
            'match': "To to",
            'replace': "To",
            'options': re.IGNORECASE
        },
        { # To of
            'name': 'To of -> Of',
            'match': "To of",
            'replace': "Of",
            'options': re.IGNORECASE
        }]
    
    def __init__(self, processor):
        _match_rules = TextTransform.match_rules
        super(TextTransform, self).__init__(_match_rules, processor)
    

class SanitizeTransform(DataTransform):
    
    match_rules = [{ 
        # em dash, e.g. '−'
        'name': 'Em dash -> dash',
        'match': u"\u2013",
        'replace': "-"
    }]
    
    def __init__(self, processor):
        _match_rules = SanitizeTransform.match_rules
        super(SanitizeTransform, self).__init__(_match_rules, processor)


class ValueTransform(DataTransform):
    
    number_formats = [
        { # range single, e.g. -(NN to NN)
            'name': 'Prepend single-range value',
            'match': r"\+?\(([0-9\.]+)%? to ([0-9\.]+)%?\)%? ", 
            'replace': r"\1-\2:"
        }, 
        { # range double, e.g. +(NN to NN
            'name': 'Prepend double-range value',
            'match': ur"\+?\(([0-9\.]+)–([0-9\.]+)%? to ([0-9\.]+)–([0-9\.]+)%?\)%? ",
            'replace': r"\1-\2,\3-\4:"
        }, 
        { # single_value, e.g. +N to ....
            'name': 'Prepend single value',
            'match': r"\+?(\d+)%?\b", 
            'replace': r"\1:"
        }]

    def __init__(self, processor):
        _match_rules = ValueTransform.number_formats
        super(ValueTransform, self).__init__(_match_rules, processor)
        
    # Override
    def apply_match_rule(self, rule, text):
        number_match = re.search(rule['match'], text)
        if number_match:
            if not self.is_transformed(text): # don't process already processed lines again
                match_groups = number_match.groups()
                value = "{0}".format(match_groups[0])
                if len(match_groups) == 4:
                    value = "{0}-{1},{2}-{3}".format(match_groups[0], match_groups[1], 
                                                 match_groups[2], match_groups[3])
                elif len(match_groups) == 2:
                    value = "{0}-{1}".format(match_groups[0], match_groups[1])
                # remove matched value from text before we extract just the words
                text = re.sub(rule['match'], "", text) 
                words = _get_words(text)
                text = "{0}:{1}".format(value, " ".join(words))
        return text
        

class UniqueItemsProcessor(object):
    
    file_header = """\
; Data from http://pathofexile.gamepedia.com/List_of_unique_items
; The "@" symbol marks a mod as implicit. This means a seperator line will \
be appended after this mod. If there are multiple implicit mods, mark the \
last one in line.{1}\
; Comments can be made with ";", blank lines will be ignored.{1}\
;{1}\
; This file was auto-generated by poe_scrape.py on {0}.{1}""" 
    category_header = "{0}; -------- {{}} ({{}}) ---------{0}{0}".format(os.linesep)
    field_separator = "|"
    value_separator = ":"
    
    def __init__(self):
        super(UniqueItemsProcessor, self).__init__()
        self.text_store = (UniqueItemsProcessor.file_header.format(time.strftime("%Y-%m-%dT%H:%M:%S"), os.linesep))
        self.item_store = {}
        self.categories = []
        self.unique_items = []
        self.special_items = []
        self.outdir = os.curdir
        self.transforms = [
            ValueTransform(self), 
            TextTransform(self)
        ]
    
    def __str__(self):
        return ("<{} at {}> - {} items: {}/{}/{} (U/S/C)"
                .format("UniqueItemsProcessor", 
                        format(id(self), '#010x' if sys.maxsize.bit_length() <= 32 else '#018x'), 
                        self._item_count(), 
                        len(self.unique_items), 
                        len(self.special_items), 
                        len(self.categories)))
    
    def _item_count(self, category=None):
        total = 0
        if category is None:
            for k in self.item_store.iterkeys():
                item_set = self.item_store[k]
                total = total + len(item_set)
        elif category in self.item_store:
            item_set = self.item_store[category]
            total = total + len(item_set)
        return total
    
    def _get_unique_item_set(self, category):
        unique_item_set = []
        if category in self.item_store:
            unique_item_set = self.item_store[category]
        else:
            self.item_store[category] = unique_item_set
        return unique_item_set

    @classmethod
    def is_special_item(cls, item):
        # xpath for special mods
        # .//*[@id='mw-content-text']/dl
        affix_mods = item['affix_mods']
        for affix_mod in affix_mods:
            if ("<Style Variant>" in affix_mod) or ("see notes" in affix_mod):
                return True
        implicit_mods = item['implicit_mods']
        for implicit_mod in implicit_mods:
            if ("<Style Variant>" in implicit_mod) or ("see notes" in implicit_mod):
                return True
        return False
        
    def set_outdir(self, outdir):
        self.outdir = outdir
    
    def _add_category(self, category):
        if category not in self.categories:
            log.msg("Start new category {}".format(category), log.DEBUG)
            self.categories.append(category)
    
    def add_special_item(self, item):
        category = item['category']
        self._add_category(category)
        if poe_scrape.DEBUG > 0:
            log.msg("Marking {0} as special item for post-processing".format(item['name']), log.INFO)
        self.special_items.append(item)
        log.msg("Category {} with {} items total"
                .format(category, self._item_count(category)), log.DEBUG)
        
    def add_unique_item(self, item):
        category = item['category']
        self._add_category(category)
        self.unique_items.append(item)
        unique_item_set = self._get_unique_item_set(category)
        name = item["name"]
        url = item["url"]
        implicit_mods = item["implicit_mods"]
        affix_mods = item["affix_mods"]
        log.msg("Adding {0} to {1}".format(name, category), log.DEBUG)
        unique_item_set.append({
            "name": name, 
            "url": url, 
            "implicit_mods": implicit_mods, 
            "affix_mods": affix_mods,
            "category": category
        })
        log.msg("Category {} with {} items total"
                .format(category, self._item_count(category)), log.DEBUG)

    def _apply_transform(self, data, category):
        # Internal: RegExr x-forms:
        #  *\+?(-)?\((-?[0-9\.]+) to (-?[0-9\.]+)\)%? *([\w ]+) -> $1$2-$3:$4
        for transform in self.transforms:
            data = transform.transform(data, category)
        return data.strip()
    
    def _process_name(self, item):
        return item["name"]
            
    def _process_implicit_mods(self, item):
        implicit_mods = item["implicit_mods"]
        num_mods = len(implicit_mods)
        if num_mods == 0:
            return ""
        sep = self.field_separator
        category = item["category"]
        processed_mods = []
        if num_mods > 1:
            for mod in implicit_mods[:-1]:
                processed_mods.append(self._apply_transform(mod, category))
            processed_mods.append("@" + self._apply_transform(implicit_mods[-1], category))
        else:
            mod = self._apply_transform(implicit_mods[0], category)
            processed_mods.append('@' + mod)
        return sep + sep.join(processed_mods)
    
    def _process_affix_mods(self, item):
        affix_mods = item["affix_mods"]
        num_mods = len(affix_mods)
        if num_mods == 0:
            return ""
        sep = self.field_separator
        category = item["category"]
        processed_mods = []
        for mod in affix_mods:
            if len(mod.strip()) == 0:
                continue
            processed_mods.append(self._apply_transform(mod, category))
        return sep + sep.join(processed_mods)
    
    def _write_category(self, category):
        with open(os.path.join(self.outdir, category + ".txt"), 'w+b') as f:
            f.write(self.text_store)

    def _write_all(self):
        outfile = os.path.join(self.outdir, "Uniques.txt")
        with open(outfile, 'w+b') as f:
            if self.text_store.strip() == self.file_header:
                log.msg("Nothing to write. All URLs dropped by in/exclude patterns?")
            else:
                log.msg("Writing data to {0}.".format(outfile), level=log.INFO)
            f.write(self.text_store)
    
    def process_special_items(self):
        log.msg("Parsing special items...", log.INFO)
        sep = self.field_separator
        for special_item in self.special_items:
            url = special_item['url']
            category = special_item['category']
            name = special_item['name']
            doc = html.parse(url)
            text_mods = doc.xpath('.//dl//dd/span')
            processed_mods = []
            for _mod in text_mods:
                mod = _mod.text
                if len(mod.strip()) == 0:
                    continue
                processed_mods.append(self._apply_transform(mod, category))
            mod_string = sep.join(processed_mods)
            pattern = "<Style Variant>"
            lines = []
            for line in self.text_store.split(os.linesep):
                if name in line:
                    lines.append(line.replace(pattern, mod_string))
                else:
                    lines.append(line)
            self.text_store = os.linesep.join(lines)
             
    def process_all(self):
        for category in self.categories:
            unique_item_set = self._get_unique_item_set(category)
            text = self.text_store
            text = text + (self.category_header.format(category, self._item_count(category)))
            if poe_scrape.DEBUG > 0:
                line_format = "{{}}{{}}{{}} ; {{}} {0}".format(os.linesep)
                for item in unique_item_set:
                    line = line_format.format(self._process_name(item),
                                              self._process_implicit_mods(item), 
                                              self._process_affix_mods(item),
                                              item["url"])
                    text = text + line
            else:
                line_format = "{{}}{{}}{{}}{0}".format(os.linesep)
                for item in unique_item_set:
                    line = line_format.format(self._process_name(item),
                                              self._process_implicit_mods(item), 
                                              self._process_affix_mods(item))
                    text = text + line
            self.text_store = text
            #self._write_category(category)
        self.process_special_items()
        self._write_all()


_g_unique_items_processor = UniqueItemsProcessor()


class PoeScrapyPipeline(object):
    
    def __init__(self):
        self.files = {}
        self.exporters = {}
        self.exporter_types = [
            (XmlItemExporter, '.xml'), 
            (PprintItemExporter, '.txt')
        ]
    
    @classmethod
    def from_crawler(cls, crawler):
        pipeline = cls()
        crawler.signals.connect(pipeline.spider_closed, signals.spider_closed)
        pipeline.outdir = crawler.settings['OUTPATH']
        pipeline.verbose = crawler.settings['VERBOSE']
        pipeline.processor = UniqueItemsProcessor()

        return pipeline
          
    def spider_closed(self, spider):
        for exporters in self.exporters.itervalues():
            for exporter in exporters:
                exporter.finish_exporting()
        for afile in self.files.itervalues():
            afile.close()
        self.processor.set_outdir(self.outdir)
        self.processor.process_all()

    def _create_outfile(self, spider, outpath):
        outfile = open(outpath, 'w+b')
        filekey = self._get_file_key(spider, outpath)
        self.files[filekey] = outfile
        return outfile
    
    def _append_outline(self, item, filekey):
        exporters = self.exporters[filekey]
        for exporter in exporters:
            if isinstance(item, list):
                for entry in list:
                    exporter.export_item(entry)
            else:
                exporter.export_item(item)
    
    def _get_outfile_path(self, spider, ext='.xml'):
        '''Must be called from process_item only, 
           otherwise spider.path will be None!
        '''
        outdir = self.outdir
        try:
            if not os.path.exists(outdir):
                os.makedirs(os.path.join(os.curdir, outdir))
            return os.path.join(outdir, "{0}{1}".format(spider.path, ext))
        except:
            return None
            
    def _get_file_key(self, spider, outpath):
        return ("{0} -> {1}".format(spider, outpath))
    
    def process_item(self, item, spider):
        outpath = self._get_outfile_path(spider)
        filekey = self._get_file_key(spider, outpath)
        if filekey in self.files:
            outfile = self.files[filekey]
        else:
            for etype in self.exporter_types:
                exporter_t_cls = etype[0]
                exporter_t_ext = etype[1]
                outpath = self._get_outfile_path(spider, exporter_t_ext)
                outfile = self._create_outfile(spider, outpath)
                exporter = exporter_t_cls(outfile)
                if filekey in self.exporters:
                    existing = self.exporters[filekey]
                    existing.append(exporter)
                    self.exporters[filekey] = existing
                else:    
                    self.exporters[filekey] = [exporter]
                exporter.start_exporting()
        self._append_outline(item, filekey)
        if self.processor.is_special_item(item):
            self.processor.add_special_item(item)
        #log.msg("self.processor = %s" % str(self.processor), log.INFO)
        self.processor.add_unique_item(item)
        return item


# class XmlExportPipeline(object):
#  
#     def __init__(self):
#         self.files = {}
#      
#     @classmethod
#     def from_crawler(cls, crawler):
#         pipeline = cls()
#         crawler.signals.connect(pipeline.spider_opened, signals.spider_opened)
#         crawler.signals.connect(pipeline.spider_closed, signals.spider_closed)
#         return pipeline
#  
#     def spider_opened(self, spider):
#         afile = open('{0}_products.xml'.format(spider.name), 'w+b')
#         self.files[spider] = afile
#         self.exporter = XmlItemExporter(afile)
#         self.exporter.start_exporting()
#  
#     def spider_closed(self, spider):
#         self.exporter.finish_exporting()
#         afile = self.files.pop(spider)
#         afile.close()
#  
#     def process_item(self, item, spider):
#         self.exporter.export_item(item)
#         return item
    