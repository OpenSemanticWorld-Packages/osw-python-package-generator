import json
import logging
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import autoflake
import black
import isort
from git import InvalidGitRepositoryError
from osw.auth import CredentialManager
from osw.core import OSW
from osw.wtsite import WtPage, WtSite

_logger = logging.getLogger(__name__)

script_version = "0.4.2"

python_code_filename = "_model.py"

# Default credentials file location (next to this module). Override via
# build_packages(cred_filepath=...) - useful when the generator is installed
# as a dependency and the credentials live in the consuming project.
pwd_file_path = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "accounts.pwd.yaml"
)

# Default OSW wiki domain. Override via build_packages(osw_domain=...) to
# target a different OSW instance (e.g. a project-specific wiki).
default_osw_domain = "wiki-dev.open-semantic-lab.org"

# Shared OSW client, lazily initialized on first use (see _get_osw). Kept
# lazy so importing this module does not trigger a wiki login / credential
# prompt - the build downloads schema packages from GitHub.
osw_obj = None


def _get_osw(
    cred_filepath: "str | Path | None" = None,
    osw_domain: "str | None" = None,
):
    """Return the shared OSW client, creating it on first use.

    cred_filepath overrides the default accounts.pwd.yaml location.
    osw_domain overrides the default OSW wiki domain.
    """
    global osw_obj
    if osw_obj is None:
        cred_path = str(cred_filepath) if cred_filepath else pwd_file_path
        osw_obj = OSW(
            site=WtSite(
                WtSite.WtSiteConfig(
                    iri=osw_domain or default_osw_domain,
                    cred_mngr=CredentialManager(cred_filepath=cred_path),
                )
            )
        )
    return osw_obj


default_repo_org = "OpenSemanticWorld-Packages"

# packages in other GitHub orgs (e.g. upstream dependencies)
repo_org_overrides = {}

# prefixes to strip when deriving python package name from schema package name
# e.g. "world.opensemantic.core" -> "opensemantic.core-python"
# packages not matching any prefix keep their full name
python_package_prefix_strip = [
    "world.",
]


def _get_repo_org(package_name: str) -> str:
    """Resolve the GitHub org for a package name."""
    for prefix, org in repo_org_overrides.items():
        if package_name.startswith(prefix):
            return org
    return default_repo_org


def _get_python_package_name(package_name: str) -> str:
    """Derive python package name from schema package name.

    E.g. "world.opensemantic.core" -> "opensemantic.core-python"
    """
    for prefix in python_package_prefix_strip:
        if package_name.startswith(prefix):
            return package_name[len(prefix) :] + "-python"
    return package_name + "-python"


def _build_request(url: str, github_token: str | None = None) -> urllib.request.Request:
    """Build a urllib Request, adding Authorization header if token is provided."""
    req = urllib.request.Request(url)
    if github_token:
        # fine-grained tokens (github_pat_) use Bearer, classic PATs (ghp_) use token
        auth_scheme = "Bearer" if github_token.startswith("github_pat_") else "token"
        req.add_header("Authorization", f"{auth_scheme} {github_token}")
        _logger.debug(
            f"Request: {url} with {auth_scheme} auth "
            f"(token prefix: {github_token[:10]}...)"
        )
    req.add_header("Accept", "application/vnd.github+json")
    return req


def get_lastest_version(
    package_name, github_token: str | None = None, repo_org: str | None = None
):
    # determine latest tag. repo_org overrides the prefix-derived org - needed
    # for *-python repos whose name no longer carries the package prefix.
    org = repo_org or _get_repo_org(package_name)
    git_url = "https://github.com/" + org + "/" + package_name
    # e.g. https://api.github.com/repos/OpenSemanticWorld-Packages/world.opensemantic.core/tags
    package_versions_url = (
        git_url.replace("github.com", "api.github.com/repos") + "/tags"
    )
    # fetch JSON document with request lib
    req = _build_request(package_versions_url, github_token)
    try:
        with urllib.request.urlopen(req) as response:
            tags = json.load(response)
            if tags:
                package_version = tags[0]["name"]
                return package_version
    except urllib.error.HTTPError as e:
        _logger.error(
            f"HTTP {e.code} fetching {package_versions_url}: {e.read().decode()}"
        )
        raise
    return None


def download_schema_package(
    package_name, package_version=None, github_token: str | None = None
) -> WtSite.ReadPagePackageResult:
    _logger.info(f"Downloading schema package {package_name} {package_version or ''}")
    # download + extract the repo zip into a temp dir, then read the page
    # package from the extracted files
    with tempfile.TemporaryDirectory() as temp_dir:
        package_dir = download_repo_zip(
            _get_repo_org(package_name),
            package_name,
            temp_dir,
            github_token=github_token,
            version=package_version,
        )
        result = _get_osw().site.read_page_package(
            WtSite.ReadPagePackageParam(storage_path=str(package_dir))
        )
    return result


def download_repo_zip(
    repo_org: str,
    repo_name: str,
    dest_root: "str | Path",
    github_token: str | None = None,
    version: str | None = None,
) -> Path:
    """Download a GitHub repo as a tag zip and extract it under dest_root.

    The GitHub ``<repo>-<version>`` wrapper folder is stripped so the result
    lives at ``<dest_root>/<repo_name>/`` (matching the on-disk layout the
    dedup and required-page checks expect). Always downloads fresh: an
    existing ``<dest_root>/<repo_name>`` is removed first. Returns that dir.
    """
    if version is None:
        version = get_lastest_version(repo_name, github_token, repo_org=repo_org)
    if version is None:
        raise ValueError(f"No tags found for {repo_org}/{repo_name}")

    zip_url = (
        f"https://github.com/{repo_org}/{repo_name}/archive/refs/tags/{version}.zip"
    )
    # S310 Audit URL open for permitted schemes.
    if not zip_url.startswith(("http:", "https:")):
        raise ValueError("URL must start with 'http:' or 'https:'")

    dest_root = Path(dest_root)
    dest_root.mkdir(parents=True, exist_ok=True)
    package_dir = dest_root / repo_name
    if package_dir.exists():
        shutil.rmtree(package_dir)  # always fetch fresh, never reuse stale

    with tempfile.TemporaryDirectory() as temp_dir:
        zip_path = os.path.join(temp_dir, "downloaded.zip")
        req = _build_request(zip_url, github_token)
        with urllib.request.urlopen(req) as response, open(zip_path, "wb") as f:  # nosec S310, url validated
            f.write(response.read())
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            zip_ref.extractall(temp_dir)
        # a tag zip extracts to a single <repo>-<version> top-level folder
        extracted = [
            Path(temp_dir) / name
            for name in os.listdir(temp_dir)
            if (Path(temp_dir) / name).is_dir()
        ]
        if not extracted:
            raise RuntimeError(f"No folder extracted from {zip_url}")
        shutil.move(str(extracted[0]), str(package_dir))

    _logger.info(f"Downloaded {repo_org}/{repo_name}@{version} -> {package_dir}")
    return package_dir


def download_schema_package_dirs(
    package_names: list[str],
    dest_root: "str | Path | None" = None,
    github_token: str | None = None,
    repo_org: str | None = None,
    repo_org_map: dict[str, str] | None = None,
) -> Path:
    """Download schema package repos (latest tag) as zips into a fresh dir.

    One subfolder per package, named after the package. The returned dir is
    suitable for ``check_required_pages(additional_package_dirs=[...])`` -
    feeding upstream packages without requiring a local sibling checkout.
    Always fetches fresh (a new temp dir unless dest_root is given).
    """
    global default_repo_org, repo_org_overrides
    if repo_org is not None:
        default_repo_org = repo_org
    if repo_org_map is not None:
        repo_org_overrides = repo_org_map

    if dest_root is None:
        dest_root = Path(tempfile.mkdtemp(prefix="osw_schema_pkgs_"))
    dest_root = Path(dest_root)
    for name in package_names:
        try:
            download_repo_zip(
                _get_repo_org(name), name, dest_root, github_token=github_token
            )
        except Exception as e:
            _logger.warning(f"Could not download schema package {name}: {e}")
    return dest_root


def is_git_repo(directory):
    """Check if directory is a git repository"""
    from git import Repo

    try:
        Repo(directory)
        return True
    except InvalidGitRepositoryError:
        return False


def commit_and_tag(directory, file_paths, commit_message, tag_name):
    """Commit a file and create a version tag in git repository using GitPython"""
    from git import Repo

    if not is_git_repo(directory):
        print(f"{directory} is not a git repository")
        return False

    try:
        # Open the repository
        repo = Repo(directory)

        # Add file to staging
        repo.index.add(file_paths)

        # Commit the file, precommit-hook disabled
        repo.index.commit(commit_message, skip_hooks=True)

        # Create version tag
        repo.create_tag(tag_name, message=f"Release {tag_name}")

        print(f"Successfully committed {file_paths} and created tag {tag_name}")
        return True

    except Exception as e:
        print(f"Error during git operation: {e}")
        return False


def generate_python_dataclasses(
    schema_pages: dict[str, WtPage],
    offline_pages: dict[str, WtPage],
    python_code_working_dir: Path,
    python_code_filename: Path,
) -> list[Path]:
    # filter schema pages
    page_list = list(schema_pages.keys())
    schema_titles = [title for title in page_list if title.startswith("Category:")]

    python_code_path = Path(python_code_working_dir)
    python_code_path /= python_code_filename
    python_code_path_v1 = Path(python_code_working_dir) / "v1"
    python_code_path_v1 /= python_code_filename
    # make sure code paths exist
    python_code_path.parent.mkdir(parents=True, exist_ok=True)
    python_code_path_v1.parent.mkdir(parents=True, exist_ok=True)
    # create empty files if the do not exists
    python_code_path.touch(exist_ok=True)
    python_code_path_v1.touch(exist_ok=True)

    res = _get_osw().fetch_schema(
        fetchSchemaParam=OSW.FetchSchemaParam(
            schema_title=schema_titles,
            offline_pages=offline_pages,
            result_model_path=python_code_path,
            mode="replace",
            generator_options={
                "output_model_type": "pydantic_v2.BaseModel",
                "disable_timestamp": True,
            },
        )
    )

    if res.warning_messages:
        for warning_message in res.warning_messages:
            _logger.warning(f"Schema fetch warning: {warning_message}")
    if res.error_messages:
        for error_message in res.error_messages:
            _logger.error(f"Schema fetch error: {error_message}")

    res = _get_osw().fetch_schema(
        fetchSchemaParam=OSW.FetchSchemaParam(
            schema_title=schema_titles,
            offline_pages=offline_pages,
            result_model_path=python_code_path_v1,
            mode="replace",
            generator_options={
                "output_model_type": "pydantic.BaseModel",
                "disable_timestamp": True,
            },
        )
    )

    # check if all fetched schemas are in the offline pages
    if set(res.fetched_schema_titles) - set(offline_pages.keys()):
        _logger.warning(
            f"Not all fetched schemas are in the offline pages: "
            f"{set(res.fetched_schema_titles) - set(offline_pages.keys())}"
        )
    if res.warning_messages:
        for warning_message in res.warning_messages:
            _logger.warning(f"Schema fetch warning: {warning_message}")
    if res.error_messages:
        for error_message in res.error_messages:
            _logger.error(f"Schema fetch error: {error_message}")

    # Fix missing allOf base classes that datamodel-code-generator dropped.
    # When a schema has allOf: [$ref-A, $ref-B], the generator sometimes
    # produces class X(B) instead of class X(A, B).
    _fix_missing_allof_bases(schema_pages, offline_pages, python_code_path)
    _fix_missing_allof_bases(schema_pages, offline_pages, python_code_path_v1)

    return [python_code_path, python_code_path_v1]


def _fix_missing_allof_bases(  # noqa: C901
    schema_pages: dict,
    offline_pages: dict,
    code_path: Path,
) -> None:
    """Ensure generated classes inherit from all allOf base classes."""

    # Build map: schema title -> expected base class names from allOf
    expected_bases = {}
    for _title, page in schema_pages.items():
        schema = page.get_slot_content("jsonschema")
        if not isinstance(schema, dict):
            continue
        class_name = schema.get("title")
        if not class_name:
            continue
        allof = schema.get("allOf", [])
        bases = []
        for entry in allof:
            ref = entry.get("$ref", "")
            # Extract Category:OSW... or Category:Item from $ref
            ref_title = ref.split("?")[0].split("/wiki/")[-1] if "/wiki/" in ref else ""
            if not ref_title:
                continue
            # Find the referenced schema's title (class name)
            ref_page = offline_pages.get(ref_title)
            if ref_page:
                ref_schema = ref_page.get_slot_content("jsonschema")
                if isinstance(ref_schema, dict) and ref_schema.get("title"):
                    bases.append(ref_schema["title"])
        if len(bases) > 1:
            expected_bases[class_name] = bases

    if not expected_bases:
        return

    content = code_path.read_text(encoding="utf-8")
    changed = False
    for class_name, bases in expected_bases.items():
        # Fix both the exact class name and numbered variants (e.g. Foo1, Foo2)
        variant_pattern = re.compile(
            r"^(class\s+" + re.escape(class_name) + r"\d*" + r"\s*\()([^)]+)(\)\s*:)",
            re.MULTILINE,
        )
        for m in variant_pattern.finditer(content):
            matched_name = (
                content[m.start() : m.end()].split("(")[0].replace("class ", "").strip()
            )
            current_bases = [b.strip() for b in m.group(2).split(",")]
            missing = [b for b in bases if b not in current_bases]
            if missing:
                new_bases = ", ".join(missing + current_bases)
                old_text = m.group(0)
                new_text = m.group(1) + new_bases + m.group(3)
                content = content.replace(old_text, new_text, 1)
                _logger.info(
                    f"Fixed {matched_name}: added missing base(s) "
                    f"{missing} -> {matched_name}({new_bases})"
                )
            changed = True

    # Fix wrong type defaults: when allOf resolution picks up the wrong
    # parent's type default (e.g. ComposedUnit gets QuantityUnit's type)
    # Build title -> page mapping
    title_to_page = {}
    for _title, page in {**schema_pages, **offline_pages}.items():
        s = page.get_slot_content("jsonschema")
        if isinstance(s, dict) and s.get("title"):
            title_to_page[s["title"]] = page
    for class_name, _bases in expected_bases.items():
        page = title_to_page.get(class_name)
        if not page:
            continue
        schema_content = page.get_slot_content("jsonschema")
        if not isinstance(schema_content, dict):
            continue
        expected_type = (
            schema_content.get("properties", {}).get("type", {}).get("default")
        )
        if not expected_type:
            continue
        expected_str = json.dumps(expected_type)
        # Find the class definition line, then find its type field
        lines = content.split("\n")
        in_class = False
        for i, line in enumerate(lines):
            if re.match(r"^class\s+" + re.escape(class_name) + r"\s*\(", line):
                in_class = True
                continue
            if in_class:
                # Detect end of class (next class or top-level code)
                if line and not line[0].isspace() and line.strip():
                    break
                m = re.match(r"(\s+)type:\s*[^=]+=\s*(\[[^\]]+\])", line)
                if m:
                    old_type = m.group(2)
                    if old_type != expected_str:
                        lines[i] = line.replace(old_type, expected_str)
                        _logger.info(
                            f"Fixed {class_name} type default: "
                            f"{old_type} -> {expected_str}"
                        )
                        changed = True
                    break
        content = "\n".join(lines)

    if changed:
        code_path.write_text(content, encoding="utf-8")


def _find_dep_python_root(
    dep_python_package_name: str,
    python_code_working_dir_root: Path,
    dependency_python_roots: list[Path],
) -> Path:
    """Find the root directory containing a dependency python package."""
    for root in [python_code_working_dir_root, *dependency_python_roots]:
        candidate = root / dep_python_package_name / "src"
        if candidate.exists():
            return root
    return python_code_working_dir_root


def replace_duplicated_classes_with_imports(  # noqa: C901
    package_index,
    package_name,
    python_code_working_dir_root,
    python_code_working_dir,
    python_code_filename,
    dependency_python_roots: list[Path] | None = None,
):
    if dependency_python_roots is None:
        dependency_python_roots = []
    for subpath in ["", "v1"]:
        imports: dict[str, str] = {}
        for dep in package_index:
            dep_python_package_name = _get_python_package_name(dep)
            if dep != package_name:
                # open the result _model.py file
                dep_root = _find_dep_python_root(
                    dep_python_package_name,
                    python_code_working_dir_root,
                    dependency_python_roots,
                )
                dep_python_code_working_dir = dep_root / dep_python_package_name / "src"
                for component in dep_python_package_name.replace("-python", "").split(
                    "."
                ):
                    dep_python_code_working_dir /= component
                if subpath != "":
                    dep_python_code_working_dir /= subpath
                with open(
                    dep_python_code_working_dir / python_code_filename,
                    encoding="utf-8",
                ) as dep_file:
                    dep_code = dep_file.read()
                    pattern = re.compile(
                        r"^class\s*([\S]*)\s*\(\s*[\S\s]*?\s*\)\s*:.*\n", re.MULTILINE
                    )  # match class definition [\s\S]*(?:[^\S\n]*\n){2,}
                    classes = re.findall(pattern, dep_code)
                _logger.info(f"Imported classes from {dep}: {classes}")

                imports[dep_python_package_name.replace("-python", "")] = classes

        _python_code_working_dir = python_code_working_dir
        if subpath != "":
            _python_code_working_dir /= subpath

        with open(
            _python_code_working_dir / python_code_filename, encoding="utf-8"
        ) as file:
            content = file.read()
        for dep_python_package_name, classes in imports.items():
            import_stms = []
            import_path = dep_python_package_name
            if subpath != "":
                import_path += "." + subpath
            for c in classes:
                content_size = len(content)
                content = re.sub(
                    r"^(class\s*"
                    + c
                    + r"\s*\(\s*[\S\s]*?\s*\)\s*:.*\n[\s\S]*?(?:[^\S\n]*\n){3,})",
                    "",
                    content,
                    count=1,
                    flags=re.MULTILINE,
                )  # replace duplicated classes
                if len(content) < content_size:
                    # add import if class was removed
                    import_stms.append(f"from {import_path} import {c}")

                # remove also any
                # <class>.update_forward_refs()
                # or
                # <class>.model_rebuild()
                content = re.sub(
                    "^" + c + re.escape(".update_forward_refs()") + "\n",
                    "",
                    content,
                    count=1,
                    flags=re.MULTILINE,
                )
                content = re.sub(
                    "^" + c + re.escape(".model_rebuild()") + "\n",
                    "",
                    content,
                    count=1,
                    flags=re.MULTILINE,
                )

            content = "\n".join(import_stms) + "\n" + content

        # deduplicate identical classes within the same file
        # e.g. Tool and Tool1 generated from the same schema via different $ref paths
        class_pattern = re.compile(
            r"^(class\s+(\w+)\s*\([^)]*\)\s*:.*\n(?:(?!^class\s).*\n)*)",
            re.MULTILINE,
        )
        class_bodies = {}  # name -> (full_match, body_without_classname)
        for m in class_pattern.finditer(content):
            full_match = m.group(1)
            class_name = m.group(2)
            # Normalize: replace the class name in the definition to compare bodies
            body = re.sub(
                r"^class\s+" + re.escape(class_name) + r"(\s*\()",
                r"class __PLACEHOLDER__\1",
                full_match,
                count=1,
            )
            class_bodies[class_name] = (full_match, body)

        for class_name, (full_match, body) in list(class_bodies.items()):
            # Check if this is a numbered variant (e.g. Tool1, IDAndCountry1)
            m = re.match(r"^(.+?)(\d+)$", class_name)
            if m:
                base_name = m.group(1)
                if base_name in class_bodies:
                    _, base_body = class_bodies[base_name]
                    if body == base_body:
                        _logger.info(
                            f"Removing duplicate class {class_name} "
                            f"(identical to {base_name})"
                        )
                        content = content.replace(full_match, "")
                        # Replace any references to the numbered class
                        content = re.sub(
                            r"\b" + re.escape(class_name) + r"\b",
                            base_name,
                            content,
                        )

        # UUID-based deduplication: detect classes that share a UUID with a
        # dependency class but have a different name (e.g., SamplingInterval
        # has the same UUID as Time from characteristics.quantitative).
        # Replace with a shallow subclass that imports the dependency class.
        uuid_pattern = re.compile(r'"uuid":\s*"([^"]+)"')
        dep_uuids = {}  # uuid -> (dep_package, class_name)
        for dep_pkg, _classes in imports.items():
            dep_root = _find_dep_python_root(
                dep_pkg + "-python",
                python_code_working_dir_root,
                dependency_python_roots,
            )
            dep_dir = dep_root / (dep_pkg + "-python") / "src"
            for component in dep_pkg.split("."):
                dep_dir /= component
            dep_file = (
                dep_dir / subpath / python_code_filename
                if subpath
                else dep_dir / python_code_filename
            )
            if dep_file.exists():
                dep_code = dep_file.read_text(encoding="utf-8")
                for cm in class_pattern.finditer(dep_code):
                    dep_class_body = cm.group(1)
                    dep_class_name = cm.group(2)
                    uuid_m = uuid_pattern.search(dep_class_body)
                    if uuid_m:
                        dep_uuids[uuid_m.group(1)] = (dep_pkg, dep_class_name)

        if dep_uuids:
            for class_name, (full_match, _body) in list(class_bodies.items()):
                uuid_m = uuid_pattern.search(full_match)
                if uuid_m and uuid_m.group(1) in dep_uuids:
                    dep_pkg, dep_class_name = dep_uuids[uuid_m.group(1)]
                    if class_name != dep_class_name:
                        import_path = dep_pkg
                        if subpath:
                            import_path += "." + subpath
                        # Check if this class is actually used elsewhere
                        # (not just its own definition). Use negative
                        # lookbehind for _ to avoid matching _ClassName
                        usage_count = (
                            len(
                                re.findall(
                                    r"(?<![_\w])" + re.escape(class_name) + r"(?!\w)",
                                    content,
                                )
                            )
                            - 1
                        )  # subtract the class definition itself
                        if usage_count <= 0:
                            # Unused - just remove entirely
                            _logger.info(
                                f"Removing unused class {class_name} "
                                f"(same UUID as {dep_class_name} "
                                f"from {dep_pkg})"
                            )
                            content = content.replace(full_match, "")
                        else:
                            _logger.info(
                                f"Replacing {class_name} with shallow "
                                f"subclass of {dep_class_name} from "
                                f"{dep_pkg} (same UUID: {uuid_m.group(1)})"
                            )
                            shallow = (
                                f"class {class_name}({dep_class_name}):\n    pass\n\n\n"
                            )
                            content = content.replace(full_match, shallow)
                            # Ensure the dep class is imported
                            import_line = f"from {import_path} import {dep_class_name}"
                            if import_line not in content:
                                content = import_line + "\n" + content

        # Replace raw OSW ID type annotations with class names from deps
        osw_id_map = {}
        for uuid_str, (_dep_pkg, dep_class_name) in dep_uuids.items():
            osw_id = "OSW" + uuid_str.replace("-", "")
            osw_id_map[osw_id] = dep_class_name
        # Also include locally defined classes
        for class_name, (full_match, _) in class_bodies.items():
            uuid_m = uuid_pattern.search(full_match)
            if uuid_m:
                osw_id = "OSW" + uuid_m.group(1).replace("-", "")
                osw_id_map[osw_id] = class_name
        for osw_id, class_name in osw_id_map.items():
            if osw_id in content:
                # Only replace in type annotations (: OSW..., list[OSW...],
                # | OSW...), not inside string literals
                # Process line by line to skip lines containing quotes
                new_lines = []
                for line in content.splitlines(True):
                    if (
                        osw_id in line
                        and '"' not in line
                        and "'" not in line
                        and not line.lstrip().startswith("#")
                    ):
                        line = re.sub(
                            r"(?<![_\w])" + re.escape(osw_id) + r"(?!\w)",
                            class_name,
                            line,
                        )
                    new_lines.append(line)
                content = "".join(new_lines)
                _logger.info(f"Replaced raw OSW ID {osw_id} with {class_name}")

        # Remove pass-only subclasses that merely re-wrap an imported class
        # e.g. class Tool1(Tool):\n    pass\n when Tool is imported
        pass_class_pattern = re.compile(
            r"^class\s+(\w+)\s*\((\w+)\)\s*:\s*\n\s+pass\s*\n+",
            re.MULTILINE,
        )
        for m in pass_class_pattern.finditer(content):
            class_name = m.group(1)
            base_name = m.group(2)
            # Check if base is imported (not defined locally)
            if re.search(
                r"^class\s+" + re.escape(base_name) + r"\s*\(", content, re.MULTILINE
            ):
                continue  # base is locally defined, keep the subclass
            # Skip SamplingInterval/RefreshInterval-style classes that
            # are intentional shallow subclasses created by UUID dedup
            # (they will later get field overrides from allOf schemas)
            # Remove model_rebuild/update_forward_refs before counting
            content = re.sub(
                r"^" + re.escape(class_name) + r"\.model_rebuild\(\)\n",
                "",
                content,
                flags=re.MULTILINE,
            )
            content = re.sub(
                r"^" + re.escape(class_name) + r"\.update_forward_refs\(\)\n",
                "",
                content,
                flags=re.MULTILINE,
            )
            # Check if this class is used anywhere besides its definition
            usage_count = (
                len(
                    re.findall(
                        r"(?<![_\w])" + re.escape(class_name) + r"(?!\w)", content
                    )
                )
                - 1
            )  # subtract class def
            if usage_count <= 0:
                _logger.info(
                    f"Removing orphaned pass-only class {class_name}({base_name})"
                )
                content = content.replace(m.group(0), "")
            else:
                # Class is used but is just a pass-only wrapper of an
                # imported base. Replace all references with the base.
                _logger.info(
                    f"Collapsing pass-only class {class_name} into "
                    f"{base_name} (replacing all references)"
                )
                content = content.replace(m.group(0), "")
                content = re.sub(
                    r"(?<![_\w])" + re.escape(class_name) + r"(?!\w)",
                    base_name,
                    content,
                )

        # Remove duplicate class definitions (same name, keep first)
        seen_classes = set()
        deduped_lines = []
        in_dup = False
        for line in content.split("\n"):
            cls_match = re.match(r"^class (\w+)\(", line)
            if cls_match:
                cls_name = cls_match.group(1)
                if cls_name in seen_classes:
                    _logger.info(
                        f"Removing duplicate class {cls_name} "
                        f"(keeping first occurrence)"
                    )
                    in_dup = True
                    continue
                seen_classes.add(cls_name)
                in_dup = False
            elif in_dup:
                # Skip lines belonging to the duplicate class
                # (indented or blank lines between classes)
                if line and not line[0].isspace() and line.strip():
                    # Non-indented non-blank line = end of dup class
                    # Check if it's a model_rebuild() for the dup
                    if re.match(r"\w+\.model_rebuild\(\)", line):
                        continue
                    if re.match(r"\w+\.update_forward_refs\(\)", line):
                        continue
                    in_dup = False
                else:
                    continue
            deduped_lines.append(line)
        content = "\n".join(deduped_lines)

        # run formatting tool black on the combined content
        # consolidate imports as well
        try:
            content = black.format_str(content, mode=black.Mode())
            # run isort to sort imports using Vertical Hanging Indent style
            content = isort.code(content, profile="black")

            content = autoflake.fix_code(content, remove_all_unused_imports=True)
        except Exception as e:
            _logger.error(f"Formatting failed: {e}")

        with open(
            _python_code_working_dir / python_code_filename, "w", encoding="utf-8"
        ) as file:
            file.write(content)


def replace_unit_enums(python_code_working_dir, python_code_filename) -> None:
    """Rewrite OSW unit enums to subclass UnitEnum instead of stdlib Enum.

    A unit enum is a generated ``class <Name>Unit(Enum)`` whose members are OSW unit
    Item ids (``Item:OSW...``). Subclassing ``UnitEnum`` (str + a registering
    metaclass, defined in ``opensemantic.characteristics.quantitative._enum``) makes
    the members str-valued and registers them in the shared unit_registry, which
    enables pint conversion (``to_unit``/``from_pint``). Non-unit enums (e.g. code
    enums, or ``<Name>Unit`` enums without unit members) keep stdlib ``Enum``.
    Applies to both pydantic versions. Idempotent.
    """
    unit_enum_import = (
        "from opensemantic.characteristics.quantitative._enum import UnitEnum"
    )
    block_re = re.compile(
        r"^class (?P<name>\w+Unit)\(Enum\):.*\n(?:[ \t].*\n|\n)*",
        re.MULTILINE,
    )

    def repl(m):
        block = m.group(0)
        if "Item:OSW" not in block:
            return block  # not a unit enum - members are not OSW unit Items
        name = m.group("name")
        return block.replace(f"class {name}(Enum):", f"class {name}(UnitEnum):", 1)

    for subpath in ["", "v1"]:
        work_dir = (
            python_code_working_dir / subpath if subpath else python_code_working_dir
        )
        model_path = work_dir / python_code_filename
        if not model_path.exists():
            continue
        text = model_path.read_text(encoding="utf-8")
        new_text = block_re.sub(repl, text)
        if new_text == text:
            continue  # no unit enums to swap
        if unit_enum_import not in new_text:
            future = "from __future__ import annotations\n"
            if future in new_text:
                new_text = new_text.replace(future, future + unit_enum_import + "\n", 1)
            else:
                new_text = unit_enum_import + "\n" + new_text
        # drop the now-unused stdlib `from enum import Enum` if nothing else
        # references a bare Enum (i.e. every enum in the file was a unit enum)
        if len(re.findall(r"(?<![A-Za-z_])Enum(?![A-Za-z_])", new_text)) == 1:
            new_text = new_text.replace("from enum import Enum\n", "", 1)
        model_path.write_text(new_text, encoding="utf-8")
        _logger.info(f"Swapped unit enums to UnitEnum in {model_path}")


def build_packages(  # noqa: C901
    packages: list[str],
    python_code_working_dir_root: Path,
    commit: bool = False,
    github_token: str | None = None,
    dependency_python_roots: list[Path] | None = None,
    repo_org: str | None = None,
    repo_org_map: dict[str, str] | None = None,
    cred_filepath: "str | Path | None" = None,
    osw_domain: "str | None" = None,
):
    global default_repo_org, repo_org_overrides
    if repo_org is not None:
        default_repo_org = repo_org
    if repo_org_map is not None:
        repo_org_overrides = repo_org_map
    # initialize the shared OSW client with the given credentials / wiki
    # domain (if any) before any download/fetch happens
    if cred_filepath is not None or osw_domain is not None:
        _get_osw(cred_filepath, osw_domain)
    for package in packages:
        package_name = package.split("@")[0]
        package_version = package.split("@")[1] if "@" in package else None
        if package_version is None:
            package_version = get_lastest_version(package_name, github_token)

        # define python repo url,
        # e.g. https://github.com/OpenSemanticWorld-Packages/opensemantic.core-python
        python_package_name = _get_python_package_name(package_name)
        # python_git_url = (
        #     "https://github.com/OpenSemanticWorld-Packages/" + python_package_name
        # )
        python_code_working_dir = (
            python_code_working_dir_root / python_package_name / "src"
        )
        for component in python_package_name.replace("-python", "").split("."):
            python_code_working_dir /= component

        # define version:
        # package version + post + builder script version
        # as int (the digits per version component)
        # e.g. package_version = 0.53.0, script_version = 0.1.0
        # => 0.53.0.post000001000
        # additional 3 digits for the build number
        # in case different runs could lead to different results
        # note: pypi truncates the post number by removing leading zeros
        run_number = 0
        python_version_number = (
            package_version
            + ".post1"
            + script_version.split(".")[0].zfill(3)
            + script_version.split(".")[1].zfill(3)
            + script_version.split(".")[2].zfill(3)
            + str(run_number).zfill(3)
        )

        _logger.info(
            f"Building package {python_package_name} version {python_version_number}"
            f"at {python_code_working_dir}"
        )

        package_index: dict[str, dict[str, WtPage]] = {}
        result = download_schema_package(package_name, package_version, github_token)
        p_key = result.package_bundle.packages.keys().__iter__().__next__()
        dependencies = result.package_bundle.packages[p_key].requiredPackages
        while len(dependencies) > 0:
            dep = dependencies.pop()
            if dep not in package_index:
                dep_result = download_schema_package(dep, github_token=github_token)
                package_index[dep] = {page.title: page for page in dep_result.pages}
                dep_p_key = (
                    dep_result.package_bundle.packages.keys().__iter__().__next__()
                )
                dependencies.extend(
                    dep_result.package_bundle.packages[dep_p_key].requiredPackages
                )

        # collect all pages from all packages in the index
        pages = {page.title: page for page in result.pages}
        offline_pages = {
            page.title: page
            for pages in package_index.values()
            for page in pages.values()
        }
        # add pages to offline pages
        offline_pages.update(pages)

        python_code_paths = generate_python_dataclasses(
            pages, offline_pages, python_code_working_dir, python_code_filename
        )

        # Dedup against dependency python packages. Resolve each dependency's
        # generated models from disk if available (local working dir or a
        # supplied dependency_python_root, e.g. an in-progress sibling
        # checkout); otherwise fetch the released *-python repo fresh from
        # GitHub. Local roots take precedence so in-progress work is not
        # shadowed by a published release; the auto-download is the fallback.
        effective_dep_roots = list(dependency_python_roots or [])
        auto_root: Path | None = None
        search_roots = [python_code_working_dir_root, *effective_dep_roots]
        for dep in package_index:
            dep_python_name = _get_python_package_name(dep)
            if any(
                (Path(root) / dep_python_name / "src").exists() for root in search_roots
            ):
                continue  # already on disk - use it, do not download
            if auto_root is None:
                auto_root = Path(tempfile.mkdtemp(prefix="osw_dep_python_"))
            try:
                download_repo_zip(
                    _get_repo_org(dep),
                    dep_python_name,
                    auto_root,
                    github_token=github_token,
                )
            except Exception as e:
                _logger.warning(
                    f"Could not auto-download python package {dep_python_name}: {e}"
                )
        if auto_root is not None:
            effective_dep_roots.append(auto_root)  # fallback, lowest precedence

        replace_duplicated_classes_with_imports(
            package_index,
            package_name,
            python_code_working_dir_root,
            python_code_working_dir,
            python_code_filename,
            dependency_python_roots=effective_dep_roots,
        )

        # rewrite OSW unit enums (class <Name>Unit(Enum) with Item:OSW members) to
        # subclass UnitEnum so they register for pint conversion
        replace_unit_enums(python_code_working_dir, python_code_filename)

        # run pre-commit hooks (formatting, linting) if available in target repo
        repo_dir = python_code_working_dir_root / python_package_name
        try:
            import subprocess

            result = subprocess.run(
                ["pre-commit", "run", "--all-files"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                _logger.info(f"pre-commit passed for {python_package_name}")
            elif result.returncode == 1:
                _logger.info(
                    f"pre-commit auto-fixed files in {python_package_name}, "
                    f"re-running to verify"
                )
                result2 = subprocess.run(
                    ["pre-commit", "run", "--all-files"],
                    cwd=repo_dir,
                    capture_output=True,
                    text=True,
                )
                if result2.returncode != 0:
                    _logger.warning(
                        f"pre-commit still failing: {result2.stdout[-500:]}"
                    )
            else:
                _logger.warning(f"pre-commit failed: {result.stderr[-500:]}")
        except FileNotFoundError:
            _logger.debug("pre-commit not available, skipping formatting hooks")

        # if the target dir is a git repo, tag it with the python package version
        if commit:
            _logger.info(
                f"Done. Tag git repo at {python_code_working_dir}"
                f" with version {python_version_number}"
            )
            if is_git_repo(python_code_working_dir_root / python_package_name):
                commit_and_tag(
                    python_code_working_dir_root / python_package_name,
                    python_code_paths,
                    "generate code from " + package,
                    python_version_number,
                )
        else:
            _logger.info(f"Done. Skipping commit and tag for {python_package_name}")


if __name__ == "__main__":
    # set log level info
    logging.basicConfig(level=logging.INFO)

    # prompt for GitHub token (optional, needed for private repos)
    _token = input("GitHub token (leave empty for public repos): ").strip() or None

    build_packages(
        # packages=["world.opensemantic.core@v0.53.2"],
        # packages=["world.opensemantic.base"],
        # packages=["world.opensemantic.lab"],
        packages=[
            # "world.opensemantic.core",
            "world.opensemantic.base",
            # "world.opensemantic.lab",
        ],
        python_code_working_dir_root=Path(__file__).parents[4] / "python_packages",
        commit=False,
        github_token=_token,
    )
