import os
import yaml
import typing
import inspect
import logging
import subprocess
import importlib.util
from functools import cached_property, lru_cache

from conans import ConanFile

from .version import Version
from .upstream_project import get_upstream_project
from .cci import cci_interface, LibPullRequest


logger = logging.getLogger(__name__)


def get_recipes_list(cci_path):
    return os.listdir(os.path.join(cci_path, "recipes"))


class RecipeError(RuntimeError):
    pass


class Status(typing.NamedTuple):
    name: str
    homepage: str
    recipe_version: Version
    upstream_version: Version
    deprecated: bool

    def update_possible(self):
        return (
            not self.upstream_version.unknown
            and not self.recipe_version.unknown
            and self.upstream_version.is_date == self.recipe_version.is_date
            and self.upstream_version > self.recipe_version
        )

    def up_to_date(self):
        return (
            not self.upstream_version.unknown
            and not self.recipe_version.unknown
            and self.upstream_version.is_date == self.recipe_version.is_date
            and self.upstream_version <= self.recipe_version
        )

    def pr_opened(self):
        pr = cci_interface.libraries_pull_requests().get(self.name)
        if pr is None:
            return None
        if pr.version == self.upstream_version or pr.version == self.recipe_version:
            return pr.pr
        return None


class Recipe:
    def __init__(self, cci_path, name):
        self.name = name
        self.path = os.path.join(cci_path, "recipes", name)
        self.config_file_path = os.path.join(self.path, "config.yml")

    def config(self):
        if not os.path.exists(self.config_file_path):
            raise RecipeError("No config.yml file")

        with open(self.config_file_path) as fil:
            return yaml.load(fil, Loader=yaml.FullLoader)

    @cached_property
    def upstream(self):
        return get_upstream_project(self)

    @property
    def versions(self):
        return [Version(v) for v in self.config()["versions"].keys()]

    @property
    def versions_folders(self):
        return {Version(k): v["folder"] for k, v in self.config()["versions"].items()}

    @property
    def most_recent_version(self):
        return sorted(self.versions)[-1]

    def status(self, recipe_version=None, upstream_version=None):
        try:
            recipe_version = recipe_version or self.most_recent_version
            recipe_upstream_version = (
                upstream_version or self.upstream.most_recent_version
            )
            homepage = self.upstream.homepage
            deprecated = getattr(
                self.conanfile_class(recipe_version), "deprecated", False
            )
        except RecipeError as exc:
            logger.debug("%s: could not find version: %s", self.name, exc)
            recipe_version = Version()
            recipe_upstream_version = Version()
            homepage = None
            deprecated = None

        return Status(
            self.name,
            homepage,
            recipe_version,
            recipe_upstream_version,
            deprecated,
        )

    @lru_cache
    def conanfile_class(self, version):
        assert isinstance(version, Version)

        version_folder_path = os.path.join(self.path, self.versions_folders[version])

        spec = importlib.util.spec_from_file_location(
            "conanfile", os.path.join(version_folder_path, "conanfile.py")
        )
        conanfile = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(conanfile)

        conanfile_main_class = None
        for symbol_name in dir(conanfile):
            symbol = getattr(conanfile, symbol_name)
            if (
                inspect.isclass(symbol)
                and issubclass(symbol, ConanFile)
                and symbol is not ConanFile
            ):
                conanfile_main_class = symbol
                break

        if conanfile_main_class is None:
            raise RecipeError("Could not find ConanFile class")

        return conanfile_main_class

    def version_exists(self, version):
        return version.fixed in self.config()["versions"]
