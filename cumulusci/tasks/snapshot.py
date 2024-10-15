import os
from typing import Dict, Literal, List, Optional
from datetime import datetime
from dateutil.parser import parse
from cumulusci.core.dependencies.dependencies import (
    InputDependencyType,
    OutputDependencyType,
)
from cumulusci.core.exceptions import (
    ScratchOrgSnapshotError,
    ScratchOrgSnapshotFailure,
)
from cumulusci.core.declarations import (
    DataDeclaration,
    DevhubDeclaration,
    OrgSnapshotDeclaration,
    TaskDeclarations,
)
from cumulusci.core.github import set_github_output
from cumulusci.core.utils import process_bool_arg, process_list_arg
from cumulusci.salesforce_api.snapshot import (
    SnapshotManager,
    SnapshotNameValidator,
    SnapshotUX,
)
from cumulusci.tasks.salesforce import BaseSalesforceApiTask
from cumulusci.tasks.devhub import BaseDevhubTask
from cumulusci.tasks.github.base import BaseGithubTask
from cumulusci.utils.hashing import hash_obj
from cumulusci.utils.options import (
    CCIOptions,
    FilePath,
    Field,
)
from cumulusci.utils.yaml.render import yaml_dump
from github3 import GitHubError
from pydantic import BaseModel
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.logging import RichHandler

ORG_SNAPSHOT_FIELDS = [
    "Id",
    "SnapshotName",
    "Description",
    "Status",
    "SourceOrg",
    "CreatedDate",
    "LastModifiedDate",
    "ExpirationDate",
    "Error",
]


class BaseCreateScratchOptions(CCIOptions):
    wait: bool = Field(
        True,
        description=(
            "Whether to wait for the snapshot creation to complete. "
            "Defaults to True. If False, the task will return immediately "
            "after creating the snapshot. Use for running in a split "
            "workflow on GitHub. Looks for the GITHUB_OUTPUT environment "
            "variable and outputs SNAPSHOT_ID=<id> to it if found for use "
            "in later steps."
        ),
    )
    snapshot_id: Optional[str] = Field(
        None,
        description=(
            "The ID of the in-progress snapshot to wait for completion. "
            "If set, the task will wait for the snapshot to complete and "
            "update the existing snapshot with the new details. Use for "
            "the second step of a split workflow on GitHub."
        ),
    )
    source_org_id: Optional[str] = Field(
        None,
        description=(
            "The Salesforce Org ID of the source org to create the snapshot from."
            "Must be a valid scratch org for snapshots in the default devhub."
            "Defaults to the org passed to the task or flow."
        ),
    )
    force_create: bool = Field(
        False,
        description=(
            "Whether to force creation of a new snapshot even if an existing "
            "snapshot with the same name and info is active. Defaults to False."
        ),
    )


class HashedValue(BaseModel):
    key: str = Field(
        ...,
        description="The key of the hashed value.",
    )
    hashed: Optional[str] = Field(
        None,
        description="The hashed value, usually generated by `cci hash *` commands or task return_values.",
    )


class HashedFlow(HashedValue):
    frozen: Optional[bool] = Field(
        False,
        description="Whether the flow is frozen. Defaults to False.",
    )
    yaml: Optional[str] = Field(
        None,
        description="The YAML representation of the flow.",
    )
    yaml_path: Optional[FilePath] = Field(
        None,
        description="The path to the YAML file containing the flow.",
    )


class HashedDependencies(HashedValue):
    source_dependencies: Optional[List[InputDependencyType]] = Field(
        None,
        description="The project dependencies used to generate the hash.",
    )
    dependencies: Optional[List[OutputDependencyType]] = Field(
        None,
        description="The resolved static dependencies used to generate the hash.",
    )


class HashOptions(CCIOptions):
    snapshot_hash: Optional[str] = Field(
        None,
        description=(
            "The hash of the tracked operations that made changes to the scratch org "
            "using the org's history if track_history is enabled. Pass a value to override"
        ),
    )
    force_hash: bool = Field(
        False,
        description=(
            "Whether to force use of the passed hash value for the snapshot_hash, overriding "
            "the snapshot hash value from the org's history if available. Defaults to False."
        ),
    )


class DescriptionDataOptions(CCIOptions):
    flows: Optional[List[HashedFlow]] = Field(
        default=None,
        description=(
            "A dictionary of flow names and their corresponding hash values. "
            "If not provided, values will be looked up from the org's history. "
            "If track_history is not enabled on the org, hashed values will be "
            "generated for all listed flows. Used to include in the snapshot description."
        ),
    )
    pull_request: Optional[int] = Field(
        None,
        description=(
            "The GitHub pull request number. If set, generated snapshot "
            "names will include the PR number and the snapshot description "
            "will contain pr:<number>."
        ),
    )


class GithubCommitStatusOptions(CCIOptions):
    create_commit_status: bool = Field(
        False,
        description=(
            "Whether to create a GitHub commit status for the snapshot. "
            "Defaults to False."
        ),
    )
    commit_status_context: str = Field(
        "Snapshot",
        description=(
            "The GitHub commit status context to use for reporting the "
            "snapshot status. If set, the task will create a commit status "
            "with the snapshot status."
        ),
    )


class GithubEnvironmentOptions(CCIOptions):
    create_environment: bool = Field(
        False,
        description=(
            "Whether to create a GitHub Environment for the snapshot. "
            "Defaults to False."
        ),
    )
    environment_prefix: str = Field(
        "Snapshot-",
        description=(
            "The prefix to use for the GitHub Environment name if create_github_environment is True"
        ),
    )


class SnapshotNameOptions(CCIOptions):
    snapshot_name: str = Field(
        ...,
        description=(
            "Name of the snapshot to create. Must be a valid snapshot name. "
            "Max 14 characters, alphanumeric, and start with a letter including project code and packaging suffix."
        ),
    )


class NameContextOptions(CCIOptions):
    include_project_code: bool = Field(
        True,
        description=(
            "Whether to include the project code as a prefix in the snapshot name. "
            "Defaults to True."
        ),
    )
    include_packaging_suffix: bool = Field(
        True,
        description=(
            "Whether to include a packaging suffix in the snapshot name. "
            "Defaults to True."
        ),
    )
    packaging_suffix: Literal["P", "U"] = Field(
        "P",
        description=(
            "The packaging suffix to use in the snapshot name. Defaults to 'P'."
        ),
    )


class BaseCreateOrgSnapshot(BaseDevhubTask, BaseGithubTask, BaseSalesforceApiTask):
    """Base class for tasks that create Scratch Org Snapshots."""

    # Peg to API Version 60.0 for OrgSnapshot object
    api_version = "60.0"
    salesforce_task = True
    declarations = TaskDeclarations(
        can_predict_hashes=True,
        can_rerun_safely=True,
        data=[
            DataDeclaration(
                reads=True,
                modifies=True,
                deletes=True,
                objects=["OrgSnapshot"],
                description="Queries, updates, and deletes OrgSnapshot records",
            )
        ],
        devhub=DevhubDeclaration(
            uses_devhub=True,
            description="The Dev Hub org is used for Scratch Org Snapshots",
        ),
        snapshots=OrgSnapshotDeclaration(
            creates=True,
            modifies=True,
            deletes=True,
            description="The Scratch Org Snapshot object in the Salesforce API",
        ),
    )

    class Options(BaseCreateScratchOptions):
        pass

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.current_snapshot_id = None
        self.snapshot_name = None
        self.devhub = None
        self.snapshots = None
        self.snapshot_id = None
        self.start_time = None

    def _init_task(self):
        super()._init_task()
        self.devhub = self._get_devhub_api()
        self.console = Console()
        self.is_github_job = os.getenv("GITHUB_ACTIONS") == "true"
        self.snapshot_name = self._generate_snapshot_name(
            self.parsed_options.snapshot_name
            if hasattr(self.parsed_options, "snapshot_name")
            else None
        )
        self.start_time = self._format_datetime(self._get_current_time())
        self.snapshots = self._init_snapshots()
        self.repo = self.get_repo()

    def _init_options(self, kwargs):
        super()._init_options(kwargs)
        if isinstance(self.Options, BaseCreateScratchOptions):
            self.parsed_options.wait = process_bool_arg(self.parsed_options.wait)
            self.parsed_options.snapshot_id = self.parsed_options.snapshot_id
            self.parsed_options.source_org_id = self.parsed_options.source_org_id
        if isinstance(self.Options, DescriptionDataOptions):
            flows = self.flow.name if self.flow else None
            if hasattr(self.parsed_options, "flows"):
                flows = self.parsed_options.flows
            if not flows and self.org_config.track_history:
                flows = [
                    flow.name
                    for flow in self.org_config.history.filtered_actions(
                        {"action_type": "Flow"}
                    )
                ]

            self.parsed_options.flows = process_list_arg(
                self.parsed_options.flows or self.flow.name if self.flow else None
            )
            self.parsed_options.pull_request = self._lookup_pull_request()
        if isinstance(self.Options, GithubCommitStatusOptions):
            self.parsed_options.create_commit_status = process_bool_arg(
                self.parsed_options.create_commit_status
            )
        if isinstance(self.Options, GithubEnvironmentOptions):
            self.parsed_options.create_environment = process_bool_arg(
                self.parsed_options.create_environment
            )

        self.description = {
            "pr": None,
            "org": self.org_config.name if self.org_config else None,
            "hash": self.org_config.history.get_snapshot_hash(),
            "commit": (
                self.project_config.repo_commit[:7]
                if self.project_config.repo_commit
                else None
            ),
            "branch": self.project_config.repo_branch,
            "flows": (
                ",".join(self.parsed_options.flows)
                if self.parsed_options.flows
                else None
            ),
        }

    def _init_snapshots(self) -> SnapshotUX:
        """Initializes a SnapshotManager and SnapshotUX instance."""
        snapshot_manager = SnapshotManager(self.devhub, self.logger)
        return SnapshotUX(snapshot_manager)

    def _run_task(self):
        skip_reason = self._should_create_snapshot()

        if skip_reason and skip_reason is not True:
            # self.logger.info(f"Skipping snapshot creation: {skip_reason}")
            # if self.parsed_options.snapshot_id:

            #     self.logger.warning(
            #         "In-progress snapshot does not meet conditions for finalization. Deleting..."
            #     )
            if self.parsed_options.force_create:
                self.console.print(
                    Panel(
                        "Forcing creation of a new snapshot even if an existing snapshot with the same name and info is active.",
                        title="Snapshot Creation Override",
                        style="yellow",
                    )
                )
            else:
                self.console.print(
                    Panel(
                        f"No snapshot creation required based on current conditions. {self.return_values.get('skip_reason','')}",
                        title="Snapshot Creation",
                        border_style="yellow",
                    )
                )
                return

        if self.parsed_options.snapshot_id:
            self.logger.info(
                "Finalizing scratch org snapshot creation for {}\n".format(
                    self.parsed_options.snapshot_id
                )
            )
        else:
            self.logger.info("Starting scratch org snapshot creation")
        snapshot_name = self._generate_snapshot_name()
        description = self._generate_snapshot_description()

        try:
            if self.parsed_options.snapshot_id:
                snapshot = self.snapshots.finalize_temp_snapshot(
                    snapshot_name=snapshot_name,
                    description=description,
                    snapshot_id=self.parsed_options.snapshot_id,
                )
            else:
                snapshot = self.snapshots.create_snapshot(
                    base_name=snapshot_name,
                    description=description,
                    source_org=self.org_config.org_id,
                    wait=self.parsed_options.wait,
                )
        except ScratchOrgSnapshotError as e:
            self.console.print(
                Panel(
                    f"Failed to create snapshot: {str(e)}",
                    title="Snapshot Creation",
                    border_style="red",
                )
            )
            if self.parsed_options.commit_status_context:
                self._create_commit_status(snapshot_name, "error")
            raise

        self.return_values["snapshot_id"] = snapshot.get("Id")
        self.return_values["snapshot_name"] = snapshot.get("SnapshotName")
        self.return_values["snapshot_description"] = snapshot.get("Description")
        self.return_values["snapshot_status"] = snapshot.get("Status")

        self._report_result(snapshot)
        set_github_output("SNAPSHOT_ID", snapshot["Id"])

        if self.is_github_job and self.parsed_options.create_commit_status:
            active = self.return_values["snapshot_status"] == "Active"
            self._create_commit_status(
                snapshot_name=(
                    snapshot_name
                    if active
                    else f"{snapshot_name} ({self.return_values['snapshot_status']})"
                ),
                state="success" if active else "error",
            )
        if self.is_github_job and self.parsed_options.environment_prefix:
            self._create_github_environment(snapshot_name)

    def _should_create_snapshot(self):
        return True

    def _lookup_pull_request(self):
        if self.parsed_options.pull_request:
            return self.parsed_options.pull_request

    def _validate_snapshot_name(self, snapshot_name):
        try:
            SnapshotNameValidator(base_name=snapshot_name)
        except ValueError as e:
            raise ScratchOrgSnapshotError(str(e)) from e

    def _generate_snapshot_name(self, name: Optional[str] = None):
        # Try snapshot_name option
        if not name:
            name = self.parsed_options.snapshot_name
        # Try branch
        if not name:
            branch = self.project_config.repo_branch
            if branch:
                if branch == self.project_config.project__git__default_branch:
                    name = branch
                elif branch.startswith(
                    self.project_config.project__git__prefix_feature
                ):
                    name = f"f{branch[len(self.project_config.project__git__prefix_feature) :]}"
        # Try commit
        if not name:
            commit = self.project_config.repo_commit
            if commit:
                name = commit[:7]

        if not name:
            raise ScratchOrgSnapshotError(
                "Unable to generate snapshot name. Please provide a snapshot name."
            )

        # Handle prefixing with project code
        project_code = ""
        if (
            hasattr(self.parsed_options, "include_project_code")
            and self.parsed_options.include_project_code
        ):
            project_code = self.project_config.project_code

        # Handle packaging suffix
        packaged_code = ""
        if (
            hasattr(self.parsed_options, "include_packaging_suffix")
            and self.parsed_options.include_packaging_suffix
        ):
            packaged_code = self.parsed_options.packaging_suffix

        # Calculate available length for snapshot name, max 14 characters to reserve space for
        available_length = 14 - len(packaged_code) - len(project_code)
        original_name = f"{name}"
        name = f"{project_code}{name[:available_length]}{packaged_code}"
        if len(original_name) > available_length:
            self.logger.warning(
                f"Snapshot name '{original_name}' exceeds maximum length of {available_length} characters. Truncated to '{name}'."
            )

        self._validate_snapshot_name(name)
        return name

    def _generate_snapshot_description(self, pr_number: Optional[int] = None):
        return (
            " ".join([f"{k}:{v}" for k, v in self.description.items() if v])
        ).strip()[:255]

    def _parse_snapshot_description(self, description: str):
        return dict(item.split(":") for item in description.split(" ") if ":" in item)

    def _check_snapshot_description(self, description):
        if isinstance(description, str):
            description = self._parse_snapshot_description(description)
        if description != self.description:
            raise ScratchOrgSnapshotFailure(
                f"Snapshot description does not match expected description.\n\n"
                f"Expected: {yaml_dump(self.description, include_types=True)}\n\nActual: {yaml_dump(description, include_types=True)}"
            )

    def _create_commit_status(self, snapshot_name, state):
        try:
            description = f"Snapshot: {snapshot_name}"
            self.repo.create_status(
                self.project_config.repo_commit,
                state,
                target_url=os.environ.get("JOB_URL"),
                description=description,
                context=self.parsed_options.commit_status_context,
            )
        except GitHubError as e:
            self.logger.error(f"Failed to create commit status: {str(e)}")
            self.console.print(
                Panel(
                    f"Failed to create commit status: {str(e)}",
                    title="Commit Status",
                    border_style="red",
                )
            )

    def _create_github_environment(self, snapshot_name):
        try:
            environment_name = (
                f"{self.parsed_options.environment_prefix}{snapshot_name}"
            )

            # Check if environment already exists
            resp = self.repo._get(f"{self.repo.url}/environments/{environment_name}")
            if resp.status_code == 404:
                self.logger.info(f"Creating new environment: {environment_name}")
                resp = self.repo._put(
                    f"{self.repo.url}/environments/{environment_name}",
                )
                resp.raise_for_status()
                self.logger.info(f"Created new environment: {environment_name}")
            else:
                self.logger.info(f"Environment '{environment_name}' already exists.")

            environment = resp.json()

            self.console.print(
                Panel(
                    f"GitHub Environment '{environment_name}' created/updated successfully!",
                    title="Environment Creation",
                    border_style="green",
                )
            )

        except Exception as e:
            self.logger.error(f"Failed to create/update GitHub Environment: {str(e)}")
            self.console.print(
                Panel(
                    f"Failed to create/update GitHub Environment: {str(e)}",
                    title="Environment Creation",
                    border_style="red",
                )
            )
            raise

    def _report_result(self, snapshot, extra: Optional[Dict[str, str]] = None):
        table = Table(title="Snapshot Details", border_style="cyan")
        table.add_column("Field", style="cyan")
        table.add_column("Value", style="magenta")

        for field in [
            "Id",
            "SnapshotName",
            "Status",
            "Description",
            "CreatedDate",
            "ExpirationDate",
        ]:
            value = snapshot.get(field, "N/A")
            if field in ["CreatedDate", "ExpirationDate"]:
                value = self._format_datetime(value)
            table.add_row(field, str(value))

        self.console.print(table)

        # Output to GitHub Actions Job Summary
        summary_file = os.getenv("GITHUB_STEP_SUMMARY")
        if summary_file:
            with open(summary_file, "a") as f:
                f.write(f"## Snapshot Creation Summary\n")
                f.write(f"- **Snapshot ID**: {snapshot.get('Id')}\n")
                f.write(f"- **Snapshot Name**: {snapshot.get('SnapshotName')}\n")
                f.write(f"- **Status**: {snapshot.get('Status')}\n")
                f.write(f"- **Description**: {snapshot.get('Description')}\n")
                f.write(
                    f"- **Created Date**: {self._format_datetime(snapshot.get('CreatedDate'))}\n"
                )
                f.write(
                    f"- **Expiration Date**: {self._format_datetime(snapshot.get('ExpirationDate'))}\n"
                )
                for key, value in (extra or {}).items():
                    f.write(f"- **{key}**: {value}\n")

    def _get_current_time(self):
        return datetime.now().isoformat()

    def _format_datetime(self, date_string):
        if date_string is None:
            return "N/A"
        dt = parse(date_string)
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    def _format_date(self, date_string):
        if date_string is None:
            return "N/A"
        dt = parse(date_string)
        return dt.strftime("%Y-%m-%d")


class CreateOrgSnapshot(BaseCreateOrgSnapshot):
    salesforce_task = False
    task_docs = """
    Creates a Scratch Org Snapshot using the Dev Hub org.
   
    **Requires** *`target-dev-hub` configured globally or for the project, used as the target Dev Hub org for Scratch Org Snapshots*.
    
    Interacts directly with the OrgSnapshot object in the Salesforce API to fully automate the process of maintaining one active snapshot per snapshot name.
    
    *Snapshot Creation Process*
    
    - **Check for an existing `active` OrgSnapshot** with the same name and recording its ID
    - **Check for an existing `in-progress` OrgSnapshot** with the same name and delete it, maintaining only one in-progress snapshot build
    - **Create a temporary snapshot** under a temporary name with the provided description
    - **Poll for completion** of the snapshot creation process
        - Or pass `--wait False` to return immediately after creating the snapshot setting SNAPSHOT_ID=<id> in GITHUB_OUTPUT and reporting the snapshot details
    
    *On Successful OrgSnapshot Completion*
    
    - Delete the existing snapshot
    - Rename the snapshot to the desired name
    - Report the snapshot details including the ID, status, and expiration date
    """

    class Options(
        BaseCreateScratchOptions,
        SnapshotNameOptions,
        NameContextOptions,
        DescriptionDataOptions,
        GithubCommitStatusOptions,
        GithubEnvironmentOptions,
    ):
        pass


class CreateHashedSnapshot(BaseCreateOrgSnapshot):
    task_docs = """
    Creates a Scratch Org Snapshot of the target org using a hash representing the org's shape and 
    operations run against it to uniquely identify the org state for looking up snapshots using the hash.
    """

    class Options(
        GithubEnvironmentOptions,
        GithubCommitStatusOptions,
        DescriptionDataOptions,
        NameContextOptions,
        BaseCreateScratchOptions,
        HashOptions,
    ):
        pass

    def _init_options(self, kwargs):
        super()._init_options(kwargs)
        if self.org_config.track_history:
            snapshot_hash = self.org_config.history.get_snapshot_hash()
            passed_hash = self.parsed_options.snapshot_hash
            if passed_hash and passed_hash != snapshot_hash:
                self.logger.warning(
                    "Passed hash does not match current dependencies hash from the org history. Using current dependencies."
                )
            self.parsed_options.snapshot_hash = snapshot_hash
        else:
            if not self.parsed_options.snapshot_hash:
                raise ScratchOrgSnapshotError(
                    "Dependencies hash required when track_history is not enabled."
                )

    def _init_task(self):
        super()._init_task()

        # Use the snapshot manager to query for an existing active snapshot
        if self._should_create_snapshot() is not True:
            self.parsed_options.snapshot_id = self.snapshots.existing_active_snapshot[
                "Id"
            ]
        else:
            self.snapshot_name = self._generate_snapshot_name()

    def _generate_snapshot_name(self, name: str | None = None):
        name = f"CCI{self.parsed_options.snapshot_hash}"
        return super()._generate_snapshot_name(name)

    def _should_create_snapshot(self):
        self.snapshots.query_existing_active_snapshot(self.snapshot_name)
        if self.snapshots.existing_active_snapshot:
            return "Existing active snapshot found"
        return True


class GithubPullRequestSnapshotOptions(CCIOptions):
    check_only: bool = Field(
        default=False,
        description="Whether to only check the conditions for creating a snapshot without creating one. Defaults to False.",
    )
    build_success: bool = Field(
        ...,
        description="Set to True if the build was successful or False for a failure. Defaults to True.",
    )
    build_fail_tests: bool = Field(
        ...,
        description="Whether the build failed due to test failures. Defaults to False",
    )
    snapshot_pr: Optional[bool] = Field(
        None,
        description="Whether to create a snapshot for feature branches with PRs",
    )
    snapshot_pr_label: Optional[str] = Field(
        None,
        description="Limit snapshot creation to only PRs with this label",
    )
    snapshot_pr_draft: Optional[bool] = Field(
        None,
        description="Whether to create snapshots for draft PRs",
    )
    snapshot_fail_pr: Optional[bool] = Field(
        None,
        description="Whether to create snapshots for failed builds on branches with an open PR",
    )
    snapshot_fail_pr_label: Optional[str] = Field(
        None,
        description="Limit failure snapshot creation to only PRs with this label",
    )
    snapshot_fail_pr_draft: Optional[bool] = Field(
        None,
        description="Whether to create snapshots for failed draft PR builds",
    )
    snapshot_fail_test_only: Optional[bool] = Field(
        None,
        description="Whether to create snapshots only for test failures",
    )
    wait: bool = Field(
        True,
        description=(
            "Whether to wait for the snapshot creation to complete. "
            "Defaults to True. If False, the task will return immediately "
            "after creating the snapshot. Use for running in a split "
            "workflow on GitHub. Looks for the GITHUB_OUTPUT environment "
            "variable and outputs SNAPSHOT_ID=<id> to it if found for use "
            "in later steps."
        ),
    )
    snapshot_id: Optional[str] = Field(
        None,
        description=(
            "The ID of the in-progress snapshot to wait for completion. "
            "If set, the task will wait for the snapshot to complete and "
            "update the existing snapshot with the new details. Use for "
            "the second step of a split workflow on GitHub."
        ),
    )
    source_org_id: Optional[str] = Field(
        None,
        description=(
            "The Salesforce Org ID of the source org to create the snapshot from."
            "Must be a valid scratch org for snapshots in the default devhub."
            "Defaults to the org passed to the task or flow."
        ),
    )


class GithubPullRequestSnapshot(BaseCreateOrgSnapshot):
    task_docs = """
    Creates a Scratch Org Snapshot for a GitHub Pull Request based on build status and conditions.
    
    **Requires** *`target-dev-hub` configured globally or for the project, used as the target Dev Hub org for Scratch Org Snapshots*.
    """

    class Options(
        GithubEnvironmentOptions,
        GithubCommitStatusOptions,
        NameContextOptions,
        GithubPullRequestSnapshotOptions,
    ):
        pass

    api_version = "60.0"
    salesforce_task = True

    def __init__(self, *args, **kwargs):
        self.pull_request = None
        super().__init__(*args, **kwargs)

    def _init_options(self, kwargs):
        super()._init_options(kwargs)
        self.parsed_options.check_only = process_bool_arg(
            self.parsed_options.check_only
        )

        self.parsed_options.build_success = process_bool_arg(
            self.parsed_options.build_success
        )
        self.parsed_options.build_fail_tests = process_bool_arg(
            self.parsed_options.build_fail_tests
        )
        self.parsed_options.wait = process_bool_arg(self.parsed_options.wait)
        self.parsed_options.snapshot_pr = process_bool_arg(
            self.parsed_options.snapshot_pr
        )
        self.parsed_options.snapshot_pr_draft = process_bool_arg(
            self.parsed_options.snapshot_pr_draft
        )
        self.parsed_options.snapshot_fail_pr = process_bool_arg(
            self.parsed_options.snapshot_fail_pr
        )
        self.parsed_options.snapshot_fail_pr_draft = process_bool_arg(
            self.parsed_options.snapshot_fail_pr_draft
        )
        self.parsed_options.snapshot_fail_test_only = process_bool_arg(
            self.parsed_options.snapshot_fail_test_only
        )

        self.console = Console()

    def _init_task(self):
        super()._init_task()
        self.pull_request = self._lookup_pull_request()

    def _run_task(self):
        if self.parsed_options.check_only:
            should_create = self._should_create_snapshot()
            if should_create is not None:
                self.console.print(
                    Panel(
                        f"Conditions not met: {should_create}",
                        title="Snapshot Creation Check",
                        border_style="yellow",
                    )
                )
                return False
            else:
                Panel(
                    "Conditions met for snapshot creation.",
                    title="Snapshot Creation Check",
                    border_style="green",
                )
                return True

    def _lookup_pull_request(self):
        pr = super()._lookup_pull_request()
        if pr:
            res = [self.repo.pull_request(pr)]
        else:
            res = self.repo.pull_requests(
                state="open",
                head=f"{self.project_config.repo_owner}:{self.project_config.repo_branch}",
            )
        for pr in res:
            self.logger.info(
                f"Checking PR: {pr.number} [{pr.state}] {pr.head.ref} -> {pr.base.ref}"
            )
            if pr.state == "open" and pr.head.ref == self.project_config.repo_branch:
                self.logger.info(f"Found PR: {pr.number}")
                return pr

    def _should_create_snapshot(self):
        is_pr = self.pull_request is not None
        self.return_values["has_pr"] = is_pr
        is_draft = self.pull_request.draft if is_pr else False
        self.return_values["pr_is_draft"] = is_draft
        pr_labels = (
            [label["name"] for label in self.pull_request.labels] if is_pr else []
        )
        has_snapshot_label = self.parsed_options.snapshot_pr_label in pr_labels
        has_snapshot_fail_label = (
            self.parsed_options.snapshot_fail_pr_label in pr_labels
        )
        self.return_values["pr_has_snapshot_label"] = has_snapshot_label
        self.return_values["pr_has_snapshot_fail_label"] = has_snapshot_fail_label

        if self.parsed_options.build_success is True:
            if not self.parsed_options.snapshot_pr:
                self.return_values["skip_reason"] = "snapshot_pr is False"
                return False
            elif not is_pr:
                self.return_values["skip_reason"] = "No pull request on the branch"
                return False
            elif self.parsed_options.snapshot_pr_label and not has_snapshot_label:
                self.return_values["skip_reason"] = (
                    "Pull request does not have snapshot label"
                )
                return False
            elif is_draft and not self.parsed_options.snapshot_pr_draft:
                self.return_values["skip_reason"] = (
                    "Pull request is draft and snapshot_pr_draft is False"
                )
                return False
            return True
        else:
            if is_pr:
                return (
                    self.parsed_options.snapshot_fail_pr
                    and (not is_draft or self.parsed_options.snapshot_fail_pr_draft)
                    and (
                        not self.parsed_options.snapshot_fail_pr_label
                        or has_snapshot_fail_label
                    )
                    and (
                        not self.parsed_options.snapshot_fail_test_only
                        or not self.parsed_options.build_fail_tests
                    )
                )
            else:
                return True

    def _generate_snapshot_name(self, name):
        pr_number = self.pull_request.number if self.pull_request else None
        name = ""
        if not name:
            name = f"Pr{pr_number}" if pr_number else name

        if self.parsed_options.build_success is False:
            if self.parsed_options.build_fail_tests:
                name = f"FTest{name}"
            else:
                name = f"Fail{name}"
        return super()._generate_snapshot_name(name)
