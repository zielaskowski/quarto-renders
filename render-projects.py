#!/usr/bin/env python3
"""Render every Quarto project into the public quarto-renders repository.

Source layout:

    <projects-root>/<project>/project.json
    <projects-root>/<project>/proj/

Output layout:

    <repo-dir>/defaults.json
    <repo-dir>/projects.json
    <repo-dir>/<project>/project.json
    <repo-dir>/<project>/_quarto.yml
    <repo-dir>/<project>/_metadata.yml
    <repo-dir>/<project>/site/            # rendered Quarto output
    <repo-dir>/<project>/explore/         # explore notebooks + XML

The generated _quarto.yml is also written into the source project, so it stays
in the repository instead of being recreated only in a temporary build tree.

Run this locally, then commit and push <repo-dir>:

    python3 vps_conf/render-projects.py
    cd /home/mi/docs/prog/hi2Z/quarto-renders
    git add -A
    git commit -m "Render Quarto projects"
    git push
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

PAGE_SUFFIXES = {".ipynb", ".qmd", ".md"}
DEFAULT_PROJECTS_ROOT = Path("/home/mi/docs/prog/hi2Z")
DEFAULT_REPO_DIR = DEFAULT_PROJECTS_ROOT / "quarto-renders"


def say(message: str) -> None:
    print(f"\n==> {message}", flush=True)


def info(message: str) -> None:
    print(f"    {message}", flush=True)


def warn(message: str) -> None:
    print(f"WARNING: {message}", file=sys.stderr, flush=True)


def die(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr, flush=True)
    raise SystemExit(1)


class ManifestError(Exception):
    """Invalid project.json or a missing source file."""


def y(value: Any) -> str:
    """Return a value as a YAML-safe scalar."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def output_url(file_rel: Path) -> str:
    return file_rel.with_suffix(".html").as_posix()


def explore_url(slug: str, notebook: Path, circuit: Path) -> str:
    return "/explore?" + urlencode(
        {
            "project": slug,
            "xoctave": notebook.as_posix(),
            "circuit": circuit.as_posix(),
        }
    )


class Project:
    def __init__(self, project_dir: Path, data: dict[str, Any]) -> None:
        self.dir = project_dir
        self.slug = project_dir.name
        self.proj = project_dir / "proj"
        self.name = str(data.get("name") or self.slug.replace("_", " ").title())
        self.pages = self._load_pages(data.get("pages"))
        self.explore = self._load_explore(data.get("explore"))

    def _load_pages(self, raw: Any) -> list[dict[str, Any]]:
        if not isinstance(raw, list) or not raw:
            raise ManifestError("'pages' must be a non-empty list")

        pages: list[dict[str, Any]] = []
        seen_files: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                raise ManifestError("each page must be an object")

            file_rel = Path(str(item.get("file") or ""))
            file_key = file_rel.as_posix()
            if not file_key or file_key == "." or file_rel.is_absolute() or ".." in file_rel.parts:
                raise ManifestError(f"page '{file_key}': invalid file path")
            if file_key in seen_files:
                raise ManifestError(f"duplicate page file: {file_key}")
            seen_files.add(file_key)

            src = (self.proj / file_rel).resolve()
            try:
                src.relative_to(self.proj.resolve())
            except ValueError as err:
                raise ManifestError(f"page '{file_key}': file escapes proj/") from err
            if not src.is_file():
                raise ManifestError(f"page '{file_key}': missing file {src}")
            if src.suffix not in PAGE_SUFFIXES:
                raise ManifestError(f"page '{file_key}': unsupported file {src}")
            if file_rel.parts and file_rel.parts[0] == "explore":
                raise ManifestError(
                    f"page '{file_key}': pages must not live under explore/"
                )

            default_title = file_rel.stem.replace("-", " ").replace("_", " ").title()
            title = str(item.get("title") or default_title)
            pages.append(
                {
                    "title": title,
                    "file": file_rel,
                    "src": src,
                    "index": bool(item.get("index", False)),
                }
            )

        indexes = [page for page in pages if page["index"]]
        if not indexes:
            pages[0]["index"] = True
        elif len(indexes) > 1:
            raise ManifestError("exactly one page may have 'index': true")

        index_page = next(page for page in pages if page["index"])
        return [index_page] + [page for page in pages if page is not index_page]

    def _load_explore(self, raw: Any) -> list[dict[str, Any]]:
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise ManifestError("'explore' must be a list")

        explore: list[dict[str, Any]] = []
        seen_files: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                raise ManifestError("each explore entry must be an object")

            file_rel = Path(str(item.get("file") or ""))
            file_key = file_rel.as_posix()
            if not file_key or file_key == "." or file_rel.is_absolute() or ".." in file_rel.parts:
                raise ManifestError(f"explore '{file_key}': invalid file path")
            if file_key in seen_files:
                raise ManifestError(f"duplicate explore file: {file_key}")
            seen_files.add(file_key)
            if file_rel.parts[0] != "explore":
                raise ManifestError(
                    f"explore '{file_key}': file must live under proj/explore/"
                )

            src = (self.proj / file_rel).resolve()
            try:
                src.relative_to(self.proj.resolve())
            except ValueError as err:
                raise ManifestError(
                    f"explore '{file_key}': file escapes proj/"
                ) from err
            if not src.is_file():
                raise ManifestError(f"explore '{file_key}': missing file {src}")
            if src.suffix != ".ipynb":
                raise ManifestError(
                    f"explore '{file_key}': file must be an .ipynb notebook"
                )

            circuit_rel = Path(
                str(item.get("circuit") or file_rel.with_suffix(".xml").as_posix())
            )
            circuit_key = circuit_rel.as_posix()
            if (
                not circuit_key
                or circuit_key == "."
                or circuit_rel.is_absolute()
                or ".." in circuit_rel.parts
            ):
                raise ManifestError(
                    f"explore '{file_key}': invalid circuit path {circuit_key}"
                )
            circuit_src = (self.proj / circuit_rel).resolve()
            try:
                circuit_src.relative_to(self.proj.resolve())
            except ValueError as err:
                raise ManifestError(
                    f"explore '{file_key}': circuit escapes proj/"
                ) from err
            if not circuit_src.is_file():
                raise ManifestError(
                    f"explore '{file_key}': missing circuit {circuit_src}"
                )

            default_title = file_rel.stem.replace("-", " ").replace("_", " ").title()
            title = str(item.get("title") or default_title)
            explore.append(
                {
                    "title": title,
                    "file": file_rel,
                    "src": src,
                    "circuit": circuit_rel,
                    "circuit_src": circuit_src,
                }
            )
        return explore

    def index_entry(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.name,
            "pages": [
                {
                    "title": page["title"],
                    "file": page["file"].as_posix(),
                    "index": page["index"],
                }
                for page in self.pages
            ],
            "explore": [
                {
                    "title": entry["title"],
                    "file": entry["file"].as_posix(),
                    "circuit": entry["circuit"].as_posix(),
                }
                for entry in self.explore
            ],
        }


def load_project(project_dir: Path) -> Project:
    manifest = project_dir / "project.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise ManifestError(f"cannot read {manifest}: {err}") from err
    if not isinstance(data, dict):
        raise ManifestError(f"{manifest}: top-level value must be an object")
    if not (project_dir / "proj").is_dir():
        raise ManifestError(f"missing Quarto root: {project_dir / 'proj'}")
    return Project(project_dir, data)


def generate_quarto_yml(project: Project, defaults: dict[str, Any]) -> str:
    sidebar_id = str(defaults["sidebar_id"])
    lines: list[str] = [
        "project:",
        "  type: website",
        "  render:",
    ]
    for page in project.pages:
        lines.append(f"    - {y(page['file'].as_posix())}")
    lines += [
        "",
        "website:",
        f"  title: {y(project.name)}",
        "  search:",
        f"    location: {y(defaults['search']['location'])}",
        f"    type: {y(defaults['search']['type'])}",
        "  sidebar:",
        f"    id: {y(sidebar_id)}",
        f"    title: {y(defaults.get('sidebar_title', 'Contents'))}",
        "    style: floating",
        "    contents:",
    ]

    for page in project.pages:
        lines += [
            f"      - text: {y(page['title'])}",
            f"        href: {y(output_url(page['file']))}",
        ]

    if project.explore:
        lines += [
            '      - section: "Explore"',
            "        contents:",
        ]
        for entry in project.explore:
            lines += [
                f"          - text: {y(entry['title'])}",
                f"            href: {y('../../../../' + explore_url(project.slug, entry['file'], entry['circuit']).lstrip('/'))}",
                '            target: "_top"',
            ]

    lines += [
        "",
        "format:",
        "  html:",
        f"    theme: {y(defaults['theme'])}",
        f"    toc: {y(defaults['toc'])}",
        f"    toc-location: {y(defaults['toc_location'])}",
        f"    toc-depth: {defaults['toc_depth']}",
        f"    code-fold: {y(defaults['code_fold'])}",
        f"    css: {y(defaults['css'])}",
        "",
    ]
    return "\n".join(lines)


def generate_metadata_yml(defaults: dict[str, Any]) -> str:
    return f"sidebar: {y(defaults['sidebar_id'])}\n"


def find_quarto() -> str:
    quarto = shutil.which("quarto")
    if quarto:
        return quarto
    candidates = sorted(Path("/opt").glob("quarto-*/bin/quarto"))
    if candidates:
        return str(candidates[-1])
    die("quarto not found in PATH")


def stage_project(project: Project, stage: Path) -> None:
    ignore = shutil.ignore_patterns(
        "_quarto.yml",
        "_metadata.yml",
        ".quarto",
        "_site",
        ".ipynb_checkpoints",
    )
    shutil.copytree(project.proj, stage, ignore=ignore)


def write_configs(project: Project, defaults: dict[str, Any], target_proj: Path) -> None:
    target_proj.mkdir(parents=True, exist_ok=True)
    (target_proj / "_quarto.yml").write_text(
        generate_quarto_yml(project, defaults), encoding="utf-8"
    )
    (target_proj / "_metadata.yml").write_text(
        generate_metadata_yml(defaults), encoding="utf-8"
    )


def copy_xml_assets(project: Project, site_dir: Path) -> int:
    img_dir = project.proj / "img"
    if not img_dir.is_dir():
        return 0
    copied = 0
    for src in sorted(img_dir.rglob("*.xml")):
        if not src.is_file():
            continue
        rel = src.relative_to(project.proj)
        dst = site_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1
    return copied


def copy_explore_payload(project: Project, project_out: Path) -> int:
    explore_out = project_out / "explore"
    if explore_out.exists():
        shutil.rmtree(explore_out)

    if not project.explore:
        return 0

    for entry in project.explore:
        for rel, src in (
            (entry["file"], entry["src"]),
            (entry["circuit"], entry["circuit_src"]),
        ):
            dst = project_out / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    src_img = project.proj / "explore" / "img"
    if src_img.is_dir():
        shutil.copytree(src_img, explore_out / "img")

    return len(project.explore)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--projects-root",
        type=Path,
        default=DEFAULT_PROJECTS_ROOT,
        help=f"directory containing <project>/project.json (default: {DEFAULT_PROJECTS_ROOT})",
    )
    parser.add_argument(
        "--repo-dir",
        type=Path,
        default=DEFAULT_REPO_DIR,
        help=f"quarto-renders checkout (default: {DEFAULT_REPO_DIR})",
    )
    parser.add_argument(
        "--defaults",
        type=Path,
        default=None,
        help="Quarto defaults JSON (default: <repo-dir>/defaults.json)",
    )
    parser.add_argument(
        "--build-dir",
        type=Path,
        default=None,
        help="parent directory for temporary build trees (default: system tmp)",
    )
    args = parser.parse_args(argv)

    projects_root = args.projects_root.expanduser().resolve()
    repo_dir = args.repo_dir.expanduser().resolve()
    defaults_path = (args.defaults or (repo_dir / "defaults.json")).expanduser().resolve()

    if not projects_root.is_dir():
        die(f"projects root is not a directory: {projects_root}")
    if not repo_dir.is_dir():
        die(f"renders repo is not a directory: {repo_dir}")
    if not (repo_dir / ".git").is_dir():
        die(f"renders repo is not a git checkout: {repo_dir}")

    try:
        defaults = json.loads(defaults_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        die(f"cannot read Quarto defaults {defaults_path}: {err}")

    quarto = find_quarto()
    if args.build_dir:
        args.build_dir.mkdir(parents=True, exist_ok=True)
    build_root = Path(
        tempfile.mkdtemp(
            prefix="hi2z-quarto-render-",
            dir=str(args.build_dir) if args.build_dir else None,
        )
    )

    projects: list[Path] = [
        project_dir
        for project_dir in sorted(projects_root.iterdir())
        if project_dir.is_dir() and (project_dir / "project.json").is_file()
    ]
    if not projects:
        warn(f"no project.json found under {projects_root}")

    say(f"quarto: {quarto}")
    info(f"projects root: {projects_root}")
    info(f"renders repo:  {repo_dir}")

    index: list[dict[str, Any]] = []
    failures = 0
    xml_copied = 0
    explore_copied = 0

    try:
        for project_dir in projects:
            say(f"project '{project_dir.name}'")
            try:
                project = load_project(project_dir)
            except ManifestError as err:
                warn(f"{project_dir.name}: {err}")
                failures += 1
                continue

            info(f"source: {project.proj}")
            info(f"pages: {len(project.pages)}; explore: {len(project.explore)}")

            # Keep the generated config in the source tree and in the repo.
            write_configs(project, defaults, project.proj)
            project_out = repo_dir / project.slug
            stale_proj = project_out / "proj"
            if stale_proj.exists():
                shutil.rmtree(stale_proj)
            write_configs(project, defaults, project_out)

            stage = build_root / "stage" / project.slug
            out = build_root / "site" / project.slug
            try:
                stage_project(project, stage)
            except OSError as err:
                warn(f"{project.slug}: staging failed: {err}")
                failures += 1
                continue

            write_configs(project, defaults, stage)

            info("run: quarto render --no-execute")
            result = subprocess.run(
                [
                    quarto,
                    "render",
                    "--no-execute",
                    "--output-dir",
                    str(out),
                ],
                cwd=str(stage),
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                warn(f"quarto render failed: '{project.slug}' (moving on)")
                if result.stdout.strip():
                    print(result.stdout, file=sys.stderr)
                if result.stderr.strip():
                    print(result.stderr, file=sys.stderr)
                failures += 1
                continue

            html_files = list(out.rglob("*.html")) if out.is_dir() else []
            if not html_files:
                warn(f"quarto produced no HTML for '{project.slug}'")
                failures += 1
                continue

            site_dir = project_out / "site"
            if site_dir.exists():
                shutil.rmtree(site_dir)
            shutil.copytree(out, site_dir)

            shutil.copy2(project_dir / "project.json", project_out / "project.json")
            explore_copied += copy_explore_payload(project, project_out)
            xml_copied += copy_xml_assets(project, site_dir)
            index.append(project.index_entry())

            info(f"rendered: {project.slug} ({len(html_files)} page(s))")

        (repo_dir / "projects.json").write_text(
            json.dumps({"projects": index}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        say("done")
        info(f"projects rendered: {len(index)}")
        info(f"explore pairs copied: {explore_copied}")
        info(f"XML files copied: {xml_copied}")
        info(f"render failures: {failures}")
        info(f"output: {repo_dir}")
        info("commit and push the renders repo when the output looks right")

        if failures:
            warn(f"{failures} project(s) failed; other projects were rendered")
            return 1
        return 0

    finally:
        shutil.rmtree(build_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
