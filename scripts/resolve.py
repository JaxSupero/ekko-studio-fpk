#!/usr/bin/env python3
"""Resolve one immutable official Studio release, never track Hermes separately."""
import argparse
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request

UPSTREAM = "EKKOLearnAI/ekko-studio"
SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def request(url, authenticated=False):
    headers = {"User-Agent": "ekko-studio-fpk", "Accept": "application/vnd.github+json"}
    token = os.environ.get("GH_TOKEN", "")
    if authenticated and token:
        if not url.startswith("https://api.github.com/"):
            raise ValueError("Refusing to send token outside GitHub API")
        headers["Authorization"] = "Bearer " + token
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
        return response.read()


def api(path):
    return json.loads(request("https://api.github.com/" + path, True))


def version_key(release):
    match = SEMVER.fullmatch(release.get("tag_name", ""))
    return tuple(map(int, match.groups())) if match else (-1, -1, -1)


def select_release(releases):
    stable = [r for r in releases if not r.get("draft") and not r.get("prerelease")
              and SEMVER.fullmatch(r.get("tag_name", ""))]
    if not stable:
        raise ValueError("No stable semantic-version Studio release found")
    # The repository also publishes Android versions (e.g. v1.1.0) carrying
    # older Web UI assets. Only tags matching their Web UI asset version are
    # Studio candidates; validate the full asset set/digests in resolve().
    stable = [r for r in stable if any(
        a.get('name') == 'hermes-web-ui-' + r['tag_name'].lstrip('v') + '.tar.gz'
        for a in r.get('assets', []))]
    if not stable:
        raise ValueError('No stable Studio release with matching Web UI assets found')
    # Upstream deliberately marks Web UI releases non-latest: do not use releases/latest.
    return max(stable, key=version_key)


def asset_metadata(release, name):
    matches = [a for a in release.get("assets", []) if a["name"] == name and a.get("state") == "uploaded"]
    if len(matches) != 1:
        raise ValueError("Official release is incomplete; missing uploaded asset: " + name)
    asset = matches[0]
    digest = asset.get("digest", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Official GitHub asset lacks a SHA-256 digest: " + name)
    url = asset["browser_download_url"]
    if not url.startswith("https://github.com/" + UPSTREAM + "/releases/download/"):
        raise ValueError("Unexpected release download origin")
    return {"name": name, "url": url, "sha256": digest.split(":", 1)[1], "size": asset["size"]}


def resolve(release, iteration):
    if not SEMVER.fullmatch(release.get("tag_name", "")) or release.get("draft") or release.get("prerelease"):
        raise ValueError("Only published stable Studio versions are supported")
    version = release["tag_name"].lstrip("v")
    commit = api("repos/" + UPSTREAM + "/commits/" + release["tag_name"])["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Unresolved upstream source commit")
    workflow = request("https://raw.githubusercontent.com/" + UPSTREAM + "/" + commit
                       + "/.github/workflows/webui-release.yml").decode()
    node_versions = set(re.findall(r"node-version:\s*['\"]?(\d+\.\d+\.\d+)", workflow))
    if len(node_versions) != 1:
        raise ValueError("Upstream must declare one exact Node version; review packaging compatibility")
    prefix = "hermes-web-ui-" + version
    return {
        "repository": UPSTREAM, "tag": release["tag_name"], "upstream_version": version,
        "version": version + "-" + str(iteration), "source_commit": commit,
        "node_version": node_versions.pop(), "fnpack_version": "1.2.1",
        "asset": asset_metadata(release, prefix + ".tar.gz"),
        "manifest_asset": asset_metadata(release, prefix + ".json"),
        "checksums_asset": asset_metadata(release, prefix + ".tar.gz.sha256"),
        "wrapper_commit": os.environ.get("GITHUB_SHA", "local"),
        "hermes_agent_policy": "upstream-managed; not independently pinned or bundled",
    }


def release_notes(release):
    """Keep upstream Markdown as data, from the same release as the assets."""
    body = release.get("body")
    if not isinstance(body, str) or not body.strip():
        body = "上游未提供此版本的更新日志，请查看原版 Release。"
    tag = release["tag_name"]
    source = "https://github.com/" + UPSTREAM + "/releases/tag/" + tag
    return body + "\n\n---\n\n## fnOS FPK 打包说明\n\n" + (
        f"- 原版更新日志来源：[Ekko Studio {tag}]({source})。\n"
        "- 非官方 Native x86_64 fnOS 分发包；原版 Release 资源与 Node 运行时均经校验和验证。\n"
        "- 本 FPK 不单独锁定、安装或自动更新 Hermes Agent；请通过原版 Studio 运行时 / Agent 管理界面安装和更新。\n"
        "- 用户配置、凭据和已下载运行时在 FPK 升级时保留。\n"
        "- 首次登录使用原版默认账号 admin / 123456，请立即修改密码，勿将默认登录暴露到互联网。\n"
        "- CI 冒烟测试不等同于真实 fnOS NAS 安装验证。\n"
        "- 原版 Ekko Studio 仍受 BSL 1.1 许可证约束。\n"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default="")
    parser.add_argument("--iteration", type=int, default=1)
    parser.add_argument("--output", default="build/metadata.json")
    parser.add_argument("--notes-output", default="build/upstream-release-notes.md")
    parser.add_argument("--check-existing", action="store_true")
    args = parser.parse_args()
    if args.iteration < 1:
        parser.error("iteration must be positive")
    if args.tag and not SEMVER.fullmatch(args.tag):
        parser.error("tag must be vX.Y.Z")
    release = (api("repos/" + UPSTREAM + "/releases/tags/" + args.tag) if args.tag else
               select_release(api("repos/" + UPSTREAM + "/releases?per_page=100")))
    metadata = resolve(release, args.iteration)
    should_build = True
    if args.check_existing:
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            parser.error("GITHUB_REPOSITORY is required for --check-existing")
        try:
            existing = api("repos/" + repository + "/releases/tags/v" + metadata["version"])
            expected = "ekko-studio_v" + metadata["version"] + "_x86_64.fpk"
            names = {a["name"] for a in existing.get("assets", []) if a.get("state") == "uploaded"}
            should_build = existing.get("draft", False) or not {expected, expected + ".sha256"} <= names
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata, indent=2) + "\n")
    notes_path = Path(args.notes_output)
    notes_path.parent.mkdir(parents=True, exist_ok=True)
    notes_path.write_text(release_notes(release))
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write("should_build=" + str(should_build).lower() + "\n")
            output.write("version=" + metadata["version"] + "\n")
            output.write("node_version=" + metadata["node_version"] + "\n")
    print(json.dumps({"tag": metadata["tag"], "source_commit": metadata["source_commit"],
                      "node_version": metadata["node_version"], "should_build": should_build}))


if __name__ == "__main__":
    main()
