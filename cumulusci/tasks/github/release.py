from cumulusci.core.dependencies.dependencies import (
    get_resolver_stack,
    get_static_dependencies,
    parse_dependencies,
)
import json
import time
from datetime import datetime

import github3.exceptions

from cumulusci.core.exceptions import GithubException
from cumulusci.core.exceptions import TaskOptionsError
from cumulusci.tasks.github.base import BaseGithubTask


class CreateRelease(BaseGithubTask):

    task_options = {
        "version": {
            "description": "The managed package version number.  Ex: 1.2",
            "required": True,
        },
        "version_id": {
            "description": "The SubscriberPackageVersionId (04t) associated with this release.",
            "required": False,
        },
        "message": {"description": "The message to attach to the created git tag"},
        "dependencies": {
            "description": "List of dependencies to record in the tag message."
        },
        "commit": {
            "description": (
                "Override the commit used to create the release. "
                "Defaults to the current local HEAD commit"
            )
        },
        "resolution_strategy": {
            "description": "The name of a sequence of resolution_strategy (from project__dependency_resolutions) to apply to dynamic dependencies. Defaults to 'production'."
        },
    }

    def _init_options(self, kwargs):
        super()._init_options(kwargs)

        self.commit = self.options.get("commit", self.project_config.repo_commit)
        if not self.commit:
            message = "Could not detect the current commit from the local repo"
            self.logger.error(message)
            raise GithubException(message)
        if len(self.commit) != 40:
            raise TaskOptionsError("The commit option must be exactly 40 characters.")

    def _run_task(self):
        repo = self.get_repo()

        version = self.options["version"]
        tag_name = self.project_config.get_tag_for_version(version)

        # Make sure release doesn't already exist
        try:
            release = repo.release_from_tag(tag_name)
        except github3.exceptions.NotFoundError:
            pass
        else:
            message = f"Release {release.name} already exists at {release.html_url}"
            self.logger.error(message)
            raise GithubException(message)

        # Build tag message
        message = self.options.get("message", "Release of version {}".format(version))
        if self.options.get("version_id"):
            message += f"\n\nversion_id: {self.options['version_id']}"
        dependencies = get_static_dependencies(
            parse_dependencies(
                self.options.get("dependencies")
                or self.project_config.project__dependencies
            ),
            get_resolver_stack(
                self.project_config,
                self.options.get("resolution_strategy") or "production",
            ),
            self.project_config,
        )
        if dependencies:
            dependencies = [d.dict(exclude_none=True) for d in dependencies]
            message += "\n\ndependencies: {}".format(json.dumps(dependencies, indent=4))

        try:
            repo.ref(f"tags/{tag_name}")
        except github3.exceptions.NotFoundError:
            # Create the annotated tag
            repo.create_tag(
                tag=tag_name,
                message=message,
                sha=self.commit,
                obj_type="commit",
                tagger={
                    "name": self.github_config.username,
                    "email": self.github_config.email,
                    "date": f"{datetime.utcnow().isoformat()}Z",
                },
                lightweight=False,
            )

            # Sleep for Github to catch up with the fact that the tag actually exists!
            time.sleep(3)

        prerelease = "Beta" in version

        # Create the Github Release
        release = repo.create_release(
            tag_name=tag_name, name=version, prerelease=prerelease
        )
        self.return_values = {
            "tag_name": tag_name,
            "name": version,
            "dependencies": dependencies,
        }
        self.logger.info(f"Created release {release.name} at {release.html_url}")
