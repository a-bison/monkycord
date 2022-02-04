#
# Config classes for dynamic, persistent configuration.
# 

import logging
import json


# Very simple config database consisting of json files on disk.
# Saves a different version of the config depending on the guild.
#
# On disk structure:
# config_root_dir \_ common.json
#                 |_ <guild_id_1>.json
#                 |_ <guild_id_2>.json
#
class JsonConfigDB:
    def __init__(self, path, template=None, main_template=None):
        self.path = path
        self.db = {}
        self.template = template
        self.main_template = main_template
        self.main_cfg = None

        if path.is_dir():
            self.load_db()
        elif path.exists():
            msg = "config {} is not a directory"
            raise FileExistsError(msg.format(str(path)))
        else: # No file or dir, so create new
            self.create_new_db()

        self.load_main_cfg()
        
    # Creates a new config DB
    def create_new_db(self):
        try:
            self.path.mkdir()
        except FileNotFoundError:
            logging.error("Parent directories of config not found.")
            raise

    def cfg_loc(self, guild_id):
        return self.path / (str(guild_id) + ".json")

    def common_cfg_loc(self):
        return self.path / "common.json"

    def load_main_cfg(self):
        loc = self.common_cfg_loc()
        self.main_cfg = JsonConfig(loc, self.main_template)

    # Loads the entire DB from a directory on disk.
    # Note that this will override any configuration currently loaded in
    # memory.
    def load_db(self):
        self.db = {}

        for child in self.path.iterdir():
            try:
                guild_id = int(child.stem)
            except ValueError:
                continue

            self.db[guild_id] = JsonConfig(self.cfg_loc(guild_id),
                                           self.template)
            logging.info("Load config: guild id {}".format(guild_id))

    def write_db(self):
        for cfg in self.db.values():
            cfg.write()

    # Gets the config for a single guild. If the config for a guild doesn't
    # exist, create it.
    def get_config(self, guild):
        if guild.id not in self.db:
            self.create_config(guild)

        return self.db[guild.id]

    # Gets state and config common to all guilds.
    def get_common_config(self):
        return self.main_cfg

    def create_config(self, guild):
        self.db[guild.id] = JsonConfig(self.cfg_loc(guild.id), self.template)


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


# Simple on-disk persistent configuration for one guild (or anything else that
# only needs one file)
class JsonConfig(ConfigMixin, SubconfigMixin):
    def __init__(self, path, template=None):
        super().__init__()

        self.opts = {}
        self.path = path
        self.template = template
        self.init()

    def init(self):
        if self.path.exists():
            self.load()
        else:
            self.create()

    def load(self):
        template = self.template

        with open(self.path, 'r') as f:
            self.opts = dict(json.load(f))

        if template is not None:
            for key, value in self.template.items():
                if key not in self.opts:
                    self.opts[key] = template[key]

    def create(self):
        if self.template is not None:
            self.opts = dict(self.template)

        self.write()

    def clear(self):
        self.opts = {}
   
    def write(self):
        with open(self.path, 'w') as f:
            json.dump(self.opts, f, indent=4)
