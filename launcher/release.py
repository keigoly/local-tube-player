"""Release を作る（版の番号 → コミット → タグ → push → GitHub の Release）。

    .venv\\Scripts\\python launcher\\release.py 0.3.1                 # 更新内容は前の Release からのコミットの題名
    .venv\\Scripts\\python launcher\\release.py 0.3.1 --notes-file notes.md
    .venv\\Scripts\\python launcher\\release.py 0.3.1 --dry-run       # 何も変えずに、作るものだけ表示する

利用者のアプリは GitHub の最新の Release を見てアップデートを知らせる（updater.py）。Release を作らない限り、
main に push しても利用者には知らせない。gh（GitHub CLI）でログインしておくこと。
"""
import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args) -> str:
    r = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        sys.exit(f"失敗しました: {' '.join(args)}\n{(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


def main() -> None:
    ap = argparse.ArgumentParser(description="MyLocalTube の Release を作る")
    ap.add_argument("version", help="新しい版（例: 0.3.1）")
    ap.add_argument("--notes-file", help="更新内容（Markdown）。無ければコミットの題名を並べる")
    ap.add_argument("--dry-run", action="store_true", help="何も変えずに、作るものだけ表示する")
    a = ap.parse_args()

    if not re.fullmatch(r"\d+\.\d+\.\d+", a.version):
        sys.exit("版は 1.2.3 の形で指定してください")
    tag = "v" + a.version
    if run("git", "rev-parse", "--abbrev-ref", "HEAD") != "main":
        sys.exit("main で実行してください")
    if run("git", "status", "--porcelain", "--untracked-files=no"):
        sys.exit("コミットしていない変更があります")
    run("git", "fetch", "origin", "--tags")
    if run("git", "tag", "-l", tag):
        sys.exit(f"{tag} はもうあります")
    if run("git", "rev-list", "--count", "HEAD..origin/main") != "0":
        sys.exit("origin/main より古いです（git pull してから実行してください）")
    prev = run("git", "describe", "--tags", "--abbrev=0") if run("git", "tag", "-l", "v*") else ""
    if a.notes_file:
        notes = Path(a.notes_file).read_text(encoding="utf-8").strip()
    else:
        subjects = run("git", "log", "--no-merges", "--format=%s", f"{prev}..HEAD" if prev else "HEAD")
        notes = "\n".join("- " + s for s in subjects.splitlines() if not s.startswith("Release v"))
    print(f"{tag} を作ります（前の Release: {prev or 'なし'}）\n--- 更新内容 ---\n{notes}\n---")
    if a.dry_run:
        return

    vf = ROOT / "VERSION"
    if vf.read_text(encoding="utf-8").strip() != a.version:
        vf.write_text(a.version + "\n", encoding="utf-8")
        run("git", "add", "VERSION")
        run("git", "commit", "-m", f"Release {tag}")
    run("git", "tag", "-a", tag, "-m", f"MyLocalTube {tag}")
    run("git", "push", "origin", "main", tag)
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(notes + "\n")
    url = run("gh", "release", "create", tag, "--title", f"MyLocalTube {tag}", "--notes-file", f.name,
              "--verify-tag")
    Path(f.name).unlink(missing_ok=True)
    print("Release を作りました:", url)


if __name__ == "__main__":
    main()
