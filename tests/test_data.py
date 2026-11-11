#!/usr/bin/env python3
"""
Tests for oreloj_data.py — "an update never loses a globe's own data".

Run from the Oreloj folder:   python3 -m unittest discover tests -v

Each test builds a tiny pretend GitHub and a pretend globe in a temporary
folder, so nothing here touches a real globe or the real GitHub.
"""
import csv, io, json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import oreloj_data as od

HEADER = "Continent,Country,City,Radio Station,Description,Website,URL Link,Favourite,Lat,Lng\n"


def row(name, url, desc="d"):
    return f"Europe,UK,London,{name},{desc},https://x,{url},,1,2\n"


def git(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t",
                        *args], capture_output=True, text=True)
    assert r.returncode == 0, (args, r.stdout, r.stderr)
    return r.stdout.strip()


def names(path):
    return [r["Radio Station"] for r in csv.DictReader(io.StringIO(Path(path).read_text()))]


def by_name(path):
    return {r["Radio Station"]: r for r in csv.DictReader(io.StringIO(Path(path).read_text()))}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.hub = t / "github"           # the pretend GitHub (work tree we commit in)
        self.hub.mkdir()
        git(self.hub, "init", "-q", "-b", "main")
        self.write_hub({
            "stations.csv": HEADER + row("Alpha", "http://a/1") + row("Beta", "http://b/1")
                            + row("Gamma", "http://c/1") + row("Delta", "http://d/1"),
            "actions.json": json.dumps({"zones": [], "presets": [{"slot": 1, "name": ""}]}),
            "calibration.csv": "code,country_iso,country_name,region,lat,lng\na0387f,GB,UK,,,\n",
            "globe.py": "PEN_VID, PEN_PID = 0x0c45, 0x7700\n",
            ".gitignore": "data/\nfavourites.json\n",
        }, "v1")
        self.globe = t / "globe"
        os.environ.pop("ORELOJ_DATA_DIR", None)

    def tearDown(self):
        self.tmp.cleanup()

    def write_hub(self, files, msg):
        for name, text in files.items():
            (self.hub / name).write_text(text)
        git(self.hub, "add", "-A")
        git(self.hub, "commit", "-q", "-m", msg)

    def clone(self):
        git(self.hub.parent, "clone", "-q", str(self.hub), str(self.globe))

    def update(self):
        return od.safe_update(self.globe, url=str(self.hub), branch="main")

    def clean(self):
        return git(self.globe, "status", "--porcelain", "--untracked-files=no") == ""


class OldGlobe(Base):
    """A globe installed before data/ existed: its edits sit in the tracked files."""

    def edit_like_an_owner(self):
        g = self.globe
        (g / "stations.csv").write_text(
            HEADER + row("Alpha", "http://a/1") + row("Beta", "http://b/1", desc="my own words")
            + row("Delta", "http://d/MINE") + row("Mine FM", "http://mine/1"))   # Gamma deleted
        (g / "actions.json").write_text(json.dumps({"zones": [{"code": "a0001f"}], "presets": []}))
        (g / "calibration.csv").write_text("code,country_iso,country_name,region,lat,lng\nb1111f,FR,France,,,\n")
        (g / "favourites.json").write_text('["Mine FM"]')

    def publish_v2(self):
        self.write_hub({
            "stations.csv": HEADER + row("Alpha", "http://a/FIXED") + row("Beta", "http://b/FIXED")
                            + row("Gamma", "http://c/FIXED") + row("Delta", "http://d/FIXED")
                            + row("Epsilon", "http://e/1"),
            "globe.py": "PEN_VID, PEN_PID = 0x0c45, 0x7700\n# v2\n",
        }, "v2")

    def test_plain_git_pull_is_blocked_by_edited_data(self):
        """The bug this module exists for."""
        self.clone(); self.edit_like_an_owner(); self.publish_v2()
        r = subprocess.run(["git", "-C", str(self.globe), "pull", "origin", "main"],
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("would be overwritten", r.stdout + r.stderr)

    def test_safe_update_keeps_everything_and_merges_fixes(self):
        self.clone(); self.edit_like_an_owner(); self.publish_v2()
        res = self.update()
        self.assertTrue(res["ok"], res); self.assertTrue(res["updated"])
        d = self.globe / "data"
        st = by_name(d / "stations.csv")
        self.assertEqual(st["Alpha"]["URL Link"], "http://a/FIXED")        # untouched → repaired
        self.assertEqual(st["Beta"]["URL Link"], "http://b/FIXED")         # repaired…
        self.assertEqual(st["Beta"]["Description"], "my own words")        # …own words kept
        self.assertEqual(st["Delta"]["URL Link"], "http://d/MINE")         # own stream kept
        self.assertNotIn("Gamma", st)                                       # deleted stays deleted
        self.assertIn("Mine FM", st)                                        # own station kept
        self.assertIn("Epsilon", st)                                        # new shared station added
        self.assertEqual(json.loads((d / "actions.json").read_text())["zones"], [{"code": "a0001f"}])
        self.assertIn("b1111f", (d / "calibration.csv").read_text())
        self.assertEqual(json.loads((d / "favourites.json").read_text()), ["Mine FM"])
        self.assertTrue(self.clean())
        self.assertIn("# v2", (self.globe / "globe.py").read_text())
        self.assertTrue(list((d / "backups").glob("local-changes-*.patch")))
        self.assertTrue(list((d / "backups").glob("stations-*.csv")))

    def test_later_updates_are_clean_and_repeatable(self):
        self.clone(); self.edit_like_an_owner(); self.publish_v2(); self.update()
        before = (self.globe / "data" / "stations.csv").read_bytes()
        res = self.update()
        self.assertTrue(res["ok"]); self.assertFalse(res["updated"])
        self.assertEqual(before, (self.globe / "data" / "stations.csv").read_bytes())
        # the owner edits again (now in data/), GitHub removes a station and changes code
        p = self.globe / "data" / "stations.csv"
        p.write_text(p.read_text() + row("Second FM", "http://second/1"))
        self.write_hub({"stations.csv": HEADER + row("Alpha", "http://a/FIXED") + row("Beta", "http://b/FIXED")
                        + row("Gamma", "http://c/FIXED") + row("Delta", "http://d/FIXED"),   # Epsilon removed
                        "globe.py": "PEN_VID, PEN_PID = 0x0c45, 0x7700\n# v3\n"}, "v3")
        res = self.update()
        self.assertTrue(res["ok"], res); self.assertTrue(res["updated"])
        got = names(p)
        self.assertNotIn("Epsilon", got); self.assertIn("Second FM", got); self.assertIn("Mine FM", got)
        self.assertTrue(self.clean())
        # and an old-style plain pull works too now
        self.write_hub({"stations.csv": HEADER + row("Alpha", "http://a/V4")}, "v4")
        git(self.globe, "pull", "-q", "origin", "main")

    def test_pen_ids_edited_into_globe_py_are_kept(self):
        self.clone()
        (self.globe / "globe.py").write_text("PEN_VID, PEN_PID = 0x1234, 0xabcd\n")
        self.publish_v2()
        res = self.update()
        self.assertTrue(res["ok"], res)
        self.assertEqual(od.pen_ids(repo=self.globe), (0x1234, 0xabcd))
        self.assertIn("0x0c45", (self.globe / "globe.py").read_text())      # code is GitHub's again
        self.assertIn("pen IDs", res["msg"])

    def test_startup_moves_data_and_tidies_without_an_update(self):
        self.clone(); self.edit_like_an_owner()
        mine = (self.globe / "stations.csv").read_bytes()
        out = od.startup(self.globe)
        self.assertIn("stations.csv", out["seeded"])
        self.assertEqual((self.globe / "data" / "stations.csv").read_bytes(), mine)
        self.assertTrue(self.clean())
        self.assertEqual(od.startup(self.globe)["seeded"], [])              # second start: nothing to do
        self.assertEqual((self.globe / "data" / "stations.csv").read_bytes(), mine)

    def test_failed_update_changes_nothing(self):
        self.clone(); self.edit_like_an_owner()
        (self.globe / "globe.py").write_text("PEN_VID, PEN_PID = 0x0c45, 0x7700\n# local tweak\n")
        self.publish_v2()
        (self.globe / "new_file.py").write_text("in the way")               # untracked, blocks the merge
        self.write_hub({"new_file.py": "from github"}, "v2b")
        head = git(self.globe, "rev-parse", "HEAD")
        res = self.update()
        self.assertFalse(res["ok"])
        self.assertEqual(git(self.globe, "rev-parse", "HEAD"), head)
        self.assertIn("# local tweak", (self.globe / "globe.py").read_text())
        self.assertIn("Mine FM", (self.globe / "data" / "stations.csv").read_text())

    def test_own_commits_are_never_overwritten(self):
        self.clone()
        (self.globe / "globe.py").write_text("# committed on the globe\n")
        git(self.globe, "commit", "-qam", "local")
        self.publish_v2()
        res = self.update()
        self.assertFalse(res["ok"])
        self.assertIn("committed on the globe", (self.globe / "globe.py").read_text())


class FreshGlobe(Base):
    def test_new_globe_starts_from_the_shared_defaults(self):
        self.clone()
        self.assertEqual(sorted(od.ensure(self.globe)), ["actions.json", "calibration.csv", "stations.csv"])
        self.assertEqual(names(self.globe / "data" / "stations.csv"), ["Alpha", "Beta", "Gamma", "Delta"])
        self.assertEqual(od.pen_ids(repo=self.globe), od.DEFAULT_PEN)
        self.assertTrue(self.clean())

    def test_broken_shared_list_is_ignored(self):
        self.clone(); od.startup(self.globe)
        self.write_hub({"stations.csv": ""}, "oops")
        res = self.update()
        self.assertTrue(res["ok"])
        self.assertEqual(names(self.globe / "data" / "stations.csv"), ["Alpha", "Beta", "Gamma", "Delta"])


class CopiedFolder(Base):
    """A globe whose ~/globe was copied by scp — not a git clone (like Oreloj 1)."""

    def make(self):
        self.globe.mkdir()
        (self.globe / "globe.py").write_text("PEN_VID, PEN_PID = 0x0c45, 0x7700\n# old code\n")
        (self.globe / "stations.csv").write_text(HEADER + row("Only Here", "http://only/1"))
        (self.globe / "actions.json").write_text('{"zones": [{"code": "c3333f"}], "presets": []}')
        (self.globe / "calibration.csv").write_text("code,country_iso,country_name,region,lat,lng\n")
        (self.globe / "favourites.json").write_text('["Only Here"]')

    def test_update_explains_instead_of_guessing(self):
        self.make()
        res = self.update()
        self.assertFalse(res["ok"]); self.assertIn("rescue", res["msg"])
        self.assertEqual(names(self.globe / "data" / "stations.csv"), ["Only Here"])

    def test_adopt_makes_a_clone_and_keeps_data_exactly(self):
        self.make()
        res = od.adopt(self.globe, url=str(self.hub), branch="main")
        self.assertTrue(res["ok"], res)
        self.assertTrue(od.is_clone(self.globe)); self.assertTrue(self.clean())
        self.assertNotIn("# old code", (self.globe / "globe.py").read_text())
        # its list is kept exactly: no shared stations are pushed onto it
        self.assertEqual(names(self.globe / "data" / "stations.csv"), ["Only Here"])
        self.assertIn("c3333f", (self.globe / "data" / "actions.json").read_text())
        # …but from now on it follows changes to the shared list
        self.write_hub({"stations.csv": HEADER + row("Alpha", "http://a/1") + row("Beta", "http://b/1")
                        + row("Gamma", "http://c/1") + row("Delta", "http://d/1") + row("Zeta", "http://z/1")}, "v2")
        res = self.update()
        self.assertTrue(res["ok"], res)
        self.assertEqual(names(self.globe / "data" / "stations.csv"), ["Only Here", "Zeta"])
        git(self.globe, "pull", "-q", "origin", "main")                     # plain pull works


if __name__ == "__main__":
    unittest.main(verbosity=2)
