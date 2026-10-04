"""Install the Jira editor into the pinned Open WebUI v0.11.4 frontend.

Only the Jira-tagged confirmation branch is changed. Fail closed when an
upstream build changes the expected branch, rather than guessing at minified JS.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import posixpath
import re
import shutil


ORIGINAL = (
    'else if(Fe==="confirmation")c(Le,C),c(Ve,!1),c(Je,!0),c(Re,[]),'
    'c(tt,Ae.title),c(Xe,Ae.message);else if(Fe==="execute"){'
)
MARKER = "/* owui-jira-review-v1 */"
REPLACEMENT = (
    'else if(Fe==="confirmation"){' + MARKER +
    'if(Ae&&Ae.jira_form){Promise.resolve().then(()=>window.openJiraConfirmation(Ae))'
    '.then(C).catch(()=>C({error:"jira_confirmation_unavailable"}));}'
    'else{c(Le,C),c(Ve,!1),c(Je,!0),c(Re,[]),c(tt,Ae.title),c(Xe,Ae.message);}}'
    'else if(Fe==="execute"){'
)
INDEX_MARKER = "<!-- owui-jira-review-assets -->"
CACHE_PATCH_VERSION = "module-graph-v1"
MANIFEST_NAME = "jira-review-manifest.json"
_GENERATED = re.compile(r"\.jira-[0-9a-f]{12}\.js$")
_TOKENS = re.compile(
    r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`',
    re.DOTALL,
)


def _module_references(source: str) -> list[tuple[int, int, str]]:
    """Read import specifiers and Vite's preload table, leaving data strings alone."""
    tokens = list(_TOKENS.finditer(source))
    masked = list(source)
    for token in tokens:
        masked[token.start():token.end()] = " " * (token.end() - token.start())
    code = "".join(masked)
    tables = []
    for table in re.finditer(r"\b([A-Za-z_$][\w$]*)\.f\s*=\s*\[", code):
        # This pinned build uses a flat m.f array inside __vite__mapDeps.
        definition = code.rfind("__vite__mapDeps", 0, table.start())
        if definition < 0 or table.start() - definition > 160:
            continue
        start = table.end()
        end = code.find("]", start)
        if end < 0:
            raise RuntimeError("Incomplete Vite module preload table")
        tables.append((start, end))
    result = []
    for token in tokens:
        text = token.group()
        if text[0] not in {"'", '"'} or "\\" in text:
            continue
        prefix = code[max(0, token.start() - 80):token.start()]
        imported = re.search(r"(?:\bfrom\s*|\bimport\s*(?:\(\s*)?)$", prefix)
        preload = any(start <= token.start() < end for start, end in tables)
        if imported or preload:
            result.append((token.start() + 1, token.end() - 1, text[1:-1]))
    return result


def _local_path(reference: str, owner: str) -> str | None:
    pathname = re.split(r"[?#]", reference, maxsplit=1)[0]
    if not pathname.endswith(".js") or ":" in pathname or pathname.startswith("//"):
        return None
    app_prefix = "/_app/immutable/"
    if app_prefix in pathname:
        return pathname[pathname.index(app_prefix) + 1:]
    if pathname.startswith("_app/immutable/"):
        return pathname
    if pathname.startswith(("./", "../")):
        return posixpath.normpath(posixpath.join(posixpath.dirname(owner), pathname))
    return None


def _replace_references(source: str, owner: str, references: list[tuple[int, int, str]],
                        mapping: dict[str, str]) -> tuple[str, int]:
    edits = []
    for start, end, reference in references:
        path = _local_path(reference, owner)
        if path not in mapping:
            continue
        old_name = posixpath.basename(path)
        new_name = posixpath.basename(mapping[path])
        # Replace only the filename in this parsed module URL; keep base paths,
        # relative prefixes, query strings and fragments exactly as supplied.
        pathname, suffix = re.match(r"([^?#]*)(.*)", reference).groups()
        if not pathname.endswith(old_name):
            raise RuntimeError("Unexpected module filename in reference")
        edits.append((start, end, pathname[:-len(old_name)] + new_name + suffix))
    for start, end, value in reversed(edits):
        source = source[:start] + value + source[end:]
    return source, len(edits)


def _index_references(index: str) -> list[tuple[int, int, str]]:
    result = _module_references(index)
    for tag in re.finditer(r"<(?:link|script)\b[^>]*>", index, re.IGNORECASE):
        text = tag.group()
        is_preload = re.search(r'\brel\s*=\s*([\'\"])modulepreload\1', text, re.IGNORECASE)
        is_module = re.search(r'\btype\s*=\s*([\'\"])module\1', text, re.IGNORECASE)
        if not (is_preload or is_module):
            continue
        attribute = re.search(r'\b(?:href|src)\s*=\s*([\'\"])(.*?)\1', text)
        if attribute:
            result.append((tag.start() + attribute.start(2), tag.start() + attribute.end(2), attribute.group(2)))
    return sorted(set(result))


def _without_asset_block(index: str) -> str:
    if INDEX_MARKER not in index:
        return index
    end_marker = "<!-- /owui-jira-review-assets -->"
    if index.count(INDEX_MARKER) != 1 or index.count(end_marker) != 1:
        raise RuntimeError("Ambiguous Jira review assets in index")
    start = index.index(INDEX_MARKER)
    end = index.index(end_marker, start) + len(end_marker)
    if index[end:end + 1] == "\n":
        end += 1
    return index[:start] + index[end:]


def install(build: Path, assets: Path) -> dict[str, str]:
    manifest_path = build / "assets" / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None
    if manifest is not None and (manifest.get("version") != 1 or not isinstance(manifest.get("mapping"), dict)):
        raise RuntimeError("Invalid Jira review module manifest")
    if manifest is not None:
        for original, generated in manifest["mapping"].items():
            if (not isinstance(original, str) or not isinstance(generated, str)
                    or not original.startswith("_app/immutable/")
                    or ".." in original.split("/")
                    or generated != original[:-3] + f'.jira-{manifest.get("digest")}.js'
                    or not re.fullmatch(r"[0-9a-f]{12}", str(manifest.get("digest")))):
                raise RuntimeError("Invalid Jira review module mapping")

    bundles = [path for path in (build / "_app" / "immutable").rglob("*.js")
               if not _GENERATED.search(path.name)]
    sources = {path.relative_to(build).as_posix(): path.read_text(encoding="utf-8") for path in bundles}
    patched = [path for path, source in sources.items() if MARKER in source]
    matches = [path for path, source in sources.items() if ORIGINAL in source]
    if patched:
        if len(patched) != 1 or matches:
            raise RuntimeError("Ambiguous existing Jira review patch")
        bundle = patched[0]
        source = sources[bundle]
        if source.count(MARKER) != 1 or source.count(REPLACEMENT) != 1:
            raise RuntimeError("Existing Jira review patch differs from the supported version")
    else:
        if len(matches) != 1:
            raise RuntimeError(f"Expected one v0.11.4 confirmation branch, found {len(matches)}")
        bundle = matches[0]
        source = sources[bundle]
        if source.count(ORIGINAL) != 1:
            raise RuntimeError("Expected one confirmation branch in the supported bundle")

    index_path = build / "index.html"
    index = index_path.read_text(encoding="utf-8")
    if index.count("</head>") != 1:
        raise RuntimeError("Expected one frontend head closing tag")
    if manifest is not None:
        index, _ = _replace_references(index, "index.html", _index_references(index),
                                       {generated: original for original, generated in manifest["mapping"].items()})
    index = _without_asset_block(index)

    references = {path: _module_references(code) for path, code in sources.items()}
    edges = {path: {_local_path(ref, path) for _, _, ref in refs} & sources.keys()
             for path, refs in references.items()}
    closure = {bundle}
    while True:
        parents = {path for path, children in edges.items() if children & closure}
        enlarged = closure | parents
        if enlarged == closure:
            break
        closure = enlarged
    index_refs = _index_references(index)
    if not any(_local_path(ref, "index.html") in closure for _, _, ref in index_refs):
        raise RuntimeError("Jira review module has no reachable frontend entry")

    effective_sources = {path: sources[path].replace(ORIGINAL, REPLACEMENT) if path == bundle else sources[path]
                         for path in sorted(closure)}
    asset_bytes = [(assets / filename).read_bytes() for filename in (
        "jira-confirmation.js", "jira-confirmation.css")]
    digest_parts = asset_bytes + [ORIGINAL.encode(), REPLACEMENT.encode(), CACHE_PATCH_VERSION.encode()]
    digest_parts += [(path + "\x00" + code).encode("utf-8") for path, code in effective_sources.items()]
    digest = hashlib.sha256(b"\x00".join(digest_parts)).hexdigest()[:12]
    mapping = {path: path[:-3] + f".jira-{digest}.js" for path in sorted(closure)}
    generated_sources = {}
    for path, code in effective_sources.items():
        generated, _ = _replace_references(code, path, _module_references(code), mapping)
        if any(_local_path(ref, mapping[path]) in closure for _, _, ref in _module_references(generated)):
            raise RuntimeError("Versioned Jira module still imports an old closure module")
        generated_sources[mapping[path]] = generated
    index, changed_index_references = _replace_references(index, "index.html", index_refs, mapping)
    if not changed_index_references:
        raise RuntimeError("Expected a versioned frontend entry reference")

    version_path = build / "_app" / "version.json"
    version_bytes = version_path.read_bytes() if version_path.exists() else None
    upstream_version_hash = hashlib.sha256(version_bytes).hexdigest() if version_bytes is not None else None
    if version_bytes is not None:
        version = json.loads(version_bytes)
        if not isinstance(version, dict) or not isinstance(version.get("version"), str):
            raise RuntimeError("Invalid Svelte frontend version")
    if manifest is not None and upstream_version_hash != manifest.get("upstream_version_sha256"):
        raise RuntimeError("Svelte frontend version differs from the Jira module manifest")
    block = (
        INDEX_MARKER + "\n"
        f'<link rel="stylesheet" href="/assets/jira-confirmation.css?v={digest}">\n'
        f'<script src="/assets/jira-confirmation.js?v={digest}"></script>\n'
        "<!-- /owui-jira-review-assets -->\n"
    )
    index = index.replace("</head>", block + "</head>")

    # All compatibility assertions above run before any frontend mutation.
    for path, code in generated_sources.items():
        (build / path).write_text(code, encoding="utf-8")
    target_assets = build / "assets"
    target_assets.mkdir(parents=True, exist_ok=True)
    for filename in ("jira-confirmation.js", "jira-confirmation.css"):
        shutil.copyfile(assets / filename, target_assets / filename)
    index_path.write_text(index, encoding="utf-8")
    # Svelte embeds the upstream version in its unchanged runtime. Keep
    # version.json byte-for-byte identical, avoiding perpetual updated=true.
    manifest_path.write_text(json.dumps({"version": 1, "digest": digest, "mapping": mapping,
                                       "upstream_version_sha256": upstream_version_hash}, indent=2, sort_keys=True), encoding="utf-8")
    return {"bundle": mapping[bundle], "original_bundle": bundle, "assets_version": digest,
            "versioned_modules": str(len(mapping))}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("build", type=Path)
    parser.add_argument("--assets", type=Path, default=Path(__file__).parent)
    args = parser.parse_args()
    print(install(args.build, args.assets))


if __name__ == "__main__":
    main()
