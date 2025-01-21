"""
Project handling routines.
"""

from __future__ import annotations

import typing

from .action import Action
from .project_config import Config
from .trigger import Trigger

if typing.TYPE_CHECKING:
    from .build import Build
    from .watcher import Watcher


class Project:
    """
    Represents a coding project.
    """

    def __init__(self, configpath: str):
        self.cfg = Config(configpath, self)

        # these will invoke a project build
        self.triggers: list[Trigger] = list()

        # these will receive or distribute build updates
        self.actions: list[Action] = list()

        # additional watchers to be attached
        self.watchers: list[Watcher] = list()

        # sort the services from the config in the appropriate list
        for service in self.cfg.services:
            if isinstance(service, Trigger):
                self.triggers.append(service)
            elif isinstance(service, Action):
                self.actions.append(service)
            else:
                raise Exception("service is not a trigger or action")

    @property
    def name(self) -> str:
        """ return the unique project name """
        return self.cfg.project_name

    def add_watchers(self, watchers: list[Watcher]):
        """ Add actions manually as they may be created by e.g. triggers. """
        self.watchers.extend(watchers)

    async def attach_actions(self, build: Build, completed: bool):
        """
        Register all actions defined in this project
        so they receives updates from the build.
        The build may be complete already and some actions
        should not be attached then.
        """
        for action in self.actions:
            watcher = await action.get_watcher(build, completed)
            if watcher:
                await build.register_watcher(watcher)

        # attach additional watchers which were created by some trigger.
        for watcher in self.watchers:
            await build.register_watcher(watcher)

    def __str__(self):
        return f"<Project name={self.name}>"
