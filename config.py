#
# Config classes for dynamic, persistent configuration.
# 

import logging
import json
from datetime import datetime

logger = logging.getLogger(__name__)


# Very simple config database consisting of json files on disk.
# Saves a different version of the config depending on the ID.
#
# On disk structure:
# config_root_dir \_ common.json
#                 |_ <id_1>.json
#                 |_ <id_2>.json
#
class JsonConfigDB:
    def __init__(self, path, template=None, unique_template=False):
        self.path = path
        self.db = {}
        self.template = template
        self.unique_template = unique_template

        if path.is_dir():
            self.load_db()
        elif path.exists():
            msg = "config {} is not a directory"
            raise FileExistsError(msg.format(str(path)))
        else:  # No file or dir, so create new
            self.create_new_db()
        
    # Creates a new config DB
    def create_new_db(self):
        try:
            self.path.mkdir()
        except FileNotFoundError:
            logger.error("Parent directories of config not found.")
            raise

    def cfg_loc(self, cid):
        return self.path / (str(cid) + ".json")

    def get_template(self, cid):
        if self.unique_template:
            cid = str(cid)

            if cid in self.template:
                return self.template[cid]
            else:
                return {}
        else:
            return self.template

    # Loads the entire DB from a directory on disk.
    # Note that this will override any configuration currently loaded in
    # memory.
    def load_db(self):
        self.db = {}

        for child in self.path.iterdir():
            try:
                cid = child.stem
            except ValueError:
                continue

            template = self.get_template(cid)
            self.db[cid] = JsonConfig(self.cfg_loc(cid), template)
            logger.info("Load config: id {}".format(cid))

    def write_db(self):
        for cfg in self.db.values():
            cfg.write()

    # Gets the config for a single guild. If the config for a guild doesn't
    # exist, create it.
    def get_config(self, cid):
        cid = str(cid)

        if cid not in self.db:
            self.create_config(cid)

        return self.db[cid]

    def create_config(self, cid):
        cid = str(cid)
        template = self.get_template(cid)

        self.db[cid] = JsonConfig(self.cfg_loc(cid), template)


# Mixin for configuration. Expects the following:
# - write() function that writes the configuration.
# - clear() function that clears the configuration.
# - a property called "opts" that allows dictionary operations.
class ConfigMixin:
    def set(self, key, value):
        self.opts[key] = value
        self.write()

    def get(self, key):
        return self.opts[key]

    def get_and_set(self, key, f):
        self.opts[key] = f(self.opts[key])
        self.write()

    def delete(self, key, ignore_keyerror=False):
        if ignore_keyerror and key not in self.opts:
            return

        del self.opts[key]
        self.write()

    # Clears an entire config, and returns a copy of what was just cleared.
    def get_and_clear(self):
        cfg = dict(self.opts)
        self.clear()
        self.write()

        return cfg

    def __contains__(self, item):
        return item in self.opts


# Enable a config to get subconfigs.
class SubconfigMixin:
    def sub(self, key):
        return SubConfig(self, key, self.opts[key])


class SubConfig(ConfigMixin, SubconfigMixin):
    def __init__(self, parent, name, cfg):
        super().__init__()

        self.parent = parent
        self.opts = cfg
        self.name = name

        self.invalid = False

    # On clear, we create a new dict in the parent and set our reference
    # to the new storage.
    def clear(self):
        self.parent.opts[self.name] = {}
        self.opts = self.parent.opts[self.name]

    def write(self):
        self.parent.write()


class ConfigException(Exception):
    pass


# Simple on-disk persistent configuration for one guild (or anything else that
# only needs one file)
#
# If check_date=True, before writing the config, we check to see if
# it's been modified after we last loaded/wrote the config. If so,
# raise an exception. Use this if you intend to edit the config manually,
# and want to make sure your modifications aren't overwritten.
class JsonConfig(ConfigMixin, SubconfigMixin):
    def __init__(self, path, template=None, check_date=False):
        super().__init__()

        self.opts = {}
        self.path = path
        self.template = template
        self.check_date = check_date
        self.last_readwrite_date = None
        self.init()

    def init(self):
        if self.path.exists():
            self.load()
        else:
            self.create()

    def __update_last_date(self):
        self.last_readwrite_date = datetime.now().timestamp()

    def load(self):
        template = self.template

        with open(self.path, 'r') as f:
            self.opts = dict(json.load(f))

        # On load, force update last date. If the json file modify
        # date has been brought past this by a manual edit, write()
        # will refuse to complete unless load() is called again.
        # (only if self.check_date=True)
        self.__update_last_date()

        if template is not None:
            template_additions = False

            for key, value in self.template.items():
                if key not in self.opts:
                    self.opts[key] = template[key]
                    template_additions = True

            # Do not write unless we make changes here.
            if template_additions:
                self.write()

    def create(self):
        if self.template is not None:
            self.opts = dict(self.template)

        self.write()

    def clear(self):
        self.opts = {}

    def write(self):
        if self.path.exists() and self.check_date:
            file_timestamp = self.path.stat().st_mtime

            # If file was modified after last load/write,
            # refuse to write.
            if file_timestamp > self.last_readwrite_date:
                msg = "{} has been modified, config must be reloaded"
                raise ConfigException(msg.format(self.path))

        with open(self.path, 'w') as f:
            json.dump(self.opts, f, indent=4)

        self.__update_last_date()
