"""transfer.py: Send to Mac. Run: python -m unittest

The end-to-end tests stand in a fake `ssh` that runs the remote command locally (with HOME set
to a temporary "Mac home"), so the real rsync, the detached pull script and the verification all
run. They're skipped when rsync isn't installed.
"""
import os, shutil, stat, subprocess, sys, tempfile, textwrap, threading, time, unittest
from pathlib import Path

import transfer

FAKE_SSH = textwrap.dedent('''\
    #!PYTHON
    # stands in for ssh: drop the options and the host, run the command locally in a fake Mac home
    import os, sys
    a, i = sys.argv[1:], 0
    while i < len(a) and a[i].startswith("-"):
        if a[i] == "--":
            i += 1
            break
        i += 2 if a[i] in ("-F", "-i", "-p", "-o", "-l") else 1
    cmd = " ".join(a[i + 1:])
    if os.environ.get("FAKE_MAC_DOWN"):
        sys.stderr.write("ssh: connect to host mac port 22: Connection refused\\n"); sys.exit(255)
    os.environ["HOME"] = os.environ["FAKE_MAC_HOME"]
    os.chdir(os.environ["HOME"])
    os.execvp("sh", ["sh", "-c", cmd])
''')


def write(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


class Pure(unittest.TestCase):
    def test_validate_defaults_and_errors(self):
        c = transfer.validate({**transfer.DEFAULTS, "host": "10.0.40.101", "user": "chris"})
        self.assertEqual((c["port"], c["mode"], c["omlx_dir"]), (22, "push", "~/.omlx/models"))
        for bad in ({"host": "-oProxyCommand=x"}, {"host": "a b"}, {"user": "x;y"}, {"port": "0"},
                    {"mode": "pull"}, {"omlx_dir": "~/a b"}, {"omlx_dir": "~/../etc"}, {"omlx_dir": "rel/path"},
                    {"rsync": "rsync; rm -rf ~"}, {"mac_source": "/Volumes/x'y"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                transfer.validate({**transfer.DEFAULTS, **bad})
        c = transfer.validate({**transfer.DEFAULTS, "mode": "pull", "mac_source": "/Volumes/llms/"})
        self.assertEqual(c["mac_source"], "/Volumes/llms")

    def test_quoting(self):
        self.assertEqual(transfer.rq("~/.omlx/models/A"), '"$HOME"/.omlx/models/A')
        self.assertEqual(transfer.rq("/Volumes/x"), "/Volumes/x")
        self.assertEqual(transfer.absolute("~/m", "/Users/c"), "/Users/c/m")
        self.assertEqual(transfer.absolute("/m", "/Users/c"), "/m")

    def test_listing_parse(self):
        got = transfer.parse_listing("10\t./a.safetensors\n5\tsub/b.json\njunk\n\nx\ty\n")
        self.assertEqual(got, {"a.safetensors": 10, "sub/b.json": 5})

    def test_compare(self):
        v = transfer.compare({"a": 10, "b": 5, "c": 1}, {"a": 10, "b": 4, ".cache/x": 1, "z": 2})
        self.assertFalse(v["ok"])
        self.assertEqual((v["files"], v["bad"], v["missing"], v["extra"]), (3, 2, 1, 1))
        self.assertEqual([p["path"] for p in v["problems"]], ["b", "c"])
        self.assertTrue(transfer.compare({"a": 1}, {"a": 1})["ok"])

    def test_pull_script_parses(self):
        s = transfer.pull_script("/Users/c/.llm-manager/transfers/abc", "/Volumes/llms/M", "/Users/c/.omlx/models/O/M", "rsync")
        r = subprocess.run(["sh", "-n"], input=s, text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.llm, self.state, self.home = self.tmp / "llms", self.tmp / "state", self.tmp / "machome"
        self.home.mkdir()
        fake = self.tmp / "bin" / "ssh"
        write(fake, FAKE_SSH.replace("PYTHON", sys.executable).encode())
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.env = {"FAKE_MAC_HOME": str(self.home)}
        os.environ.update(self.env)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("FAKE_MAC_HOME", "FAKE_MAC_DOWN")])
        self.patch(transfer, SSH_BIN=str(fake), POLL=0.1, PUSH_RETRY_WAIT=0, PULL_RETRY_WAIT=0)
        # a model folder as the downloader leaves it
        m = self.llm / "Qwen-Test"
        write(m / "model-00001.safetensors", os.urandom(300_000))
        write(m / "model-00002.safetensors", os.urandom(200_000))
        write(m / "config.json", b"{}")
        write(m / "sub" / "tokenizer.json", b"x" * 1000)
        write(m / ".hf-repo", b"Org/Qwen-Test\nabc\n\n\n")
        write(m / ".cache" / "llm-manager" / "verified-abc.txt", b"ok")
        self.mgr = transfer.Manager(self.state, self.llm)
        self.meta = {"repo": "Org/Qwen-Test", "variant": None}

    def patch(self, mod, **kw):
        for k, v in kw.items():
            old = getattr(mod, k)
            setattr(mod, k, v)
            self.addCleanup(setattr, mod, k, old)

    def configure(self, **kw):
        return self.mgr.save_config({"host": "mac.local", "user": "chris", **kw})

    def run_one(self, j, timeout=30, mgr=None):
        t = threading.Thread(target=(mgr or self.mgr).run, args=(j,), daemon=True)
        t.start()
        t.join(timeout)
        self.assertFalse(t.is_alive(), "transfer didn't finish")
        return j


class Queue(Base):
    def test_needs_settings(self):
        with self.assertRaisesRegex(ValueError, "Settings"):
            self.mgr.add("Qwen-Test", meta=self.meta)

    def test_destinations_match_the_ui(self):
        c = self.configure(lmstudio_dir="/Volumes/fast/lms")
        self.assertEqual(self.mgr.dest_for(c, "Qwen-Test", "Org/Qwen-Test", None, "omlx"), "~/.omlx/models/Org/Qwen-Test")
        self.assertEqual(self.mgr.dest_for(c, "M-Q4_K_M", "Org/M-GGUF", "Q4_K_M", "lmstudio"), "/Volumes/fast/lms/Org/M-Q4_K_M")
        self.assertEqual(self.mgr.dest_for(c, "x", None, None, "custom", "~/models/"), "~/models/x")
        with self.assertRaises(ValueError):
            self.mgr.dest_for(c, "x", None, None, "custom", "~/my models")

    def test_add_guards(self):
        self.configure()
        for name in ("nope", "../etc", ".cache", ""):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.mgr.add(name, meta=self.meta)
        write(self.llm / "Qwen-Test" / ".cache" / "llm-manager" / "x.part", b"1")
        with self.assertRaisesRegex(ValueError, "unfinished"):
            self.mgr.add("Qwen-Test", meta=self.meta)
        (self.llm / "Qwen-Test" / ".cache" / "llm-manager" / "x.part").unlink()
        busy = transfer.Manager(self.state, self.llm, busy=lambda n: True)
        with self.assertRaisesRegex(ValueError, "downloading"):
            busy.add("Qwen-Test", meta=self.meta)
        j = self.mgr.add("Qwen-Test", meta=self.meta)
        with self.assertRaisesRegex(ValueError, "already"):
            self.mgr.add("Qwen-Test", meta=self.meta)
        self.assertTrue(self.mgr.active_for("Qwen-Test"))
        self.mgr.cancel(j["id"])
        self.assertEqual(j["status"], "cancelled")
        self.mgr.retry(j["id"])
        self.assertEqual(j["status"], "queued")
        self.mgr.remove(j["id"])  # active: kept
        self.assertEqual(len(self.mgr.snapshot()), 1)

    def test_restart_requeues(self):
        self.configure()
        j = self.mgr.add("Qwen-Test", meta=self.meta)
        j["status"] = "running"
        self.mgr.save()
        self.assertEqual(transfer.Manager(self.state, self.llm).jobs[0]["status"], "queued")

    def test_settings_persist(self):
        self.configure(port=2222, mode="pull", mac_source="/Volumes/llms")
        c = transfer.Manager(self.state, self.llm).config()
        self.assertEqual((c["host"], c["port"], c["mode"]), ("mac.local", 2222, "pull"))


@unittest.skipUnless(shutil.which("rsync"), "rsync not installed")
class EndToEnd(Base):
    def dest(self):
        return self.home / ".omlx" / "models" / "Org" / "Qwen-Test"

    def assert_copied(self, j):
        self.assertEqual(j["status"], "done", j.get("error"))
        self.assertTrue(j["verify"]["ok"])
        self.assertEqual(j["verify"]["files"], 4)
        self.assertEqual(j["bytes"], j["total"])
        src = self.llm / "Qwen-Test"
        for f in ("model-00001.safetensors", "model-00002.safetensors", "config.json", "sub/tokenizer.json"):
            self.assertEqual((self.dest() / f).read_bytes(), (src / f).read_bytes())
        self.assertFalse((self.dest() / ".hf-repo").exists())
        self.assertFalse((self.dest() / ".cache").exists())

    def test_push(self):
        self.configure()
        j = self.run_one(self.mgr.add("Qwen-Test", meta=self.meta))
        self.assert_copied(j)
        self.assertEqual(j["dest_abs"], str(self.dest()))
        self.assertIn("verify: 4/4 files match", self.mgr.log_path(j["id"]).read_text())

    def test_pull(self):
        self.configure(mode="pull", mac_source=str(self.llm))  # the fake Mac sees the share at its real path
        j = self.run_one(self.mgr.add("Qwen-Test", meta=self.meta))
        self.assert_copied(j)
        self.assertEqual(j["mode"], "pull")
        self.assertIn("rsync attempt 1", self.mgr.log_path(j["id"]).read_text())

    def test_pull_without_the_share_mounted(self):
        self.configure(mode="pull", mac_source="/Volumes/not-mounted")
        j = self.run_one(self.mgr.add("Qwen-Test", meta=self.meta))
        self.assertEqual(j["status"], "failed")
        self.assertIn("isn't there on the Mac", j["error"])

    def test_retry_finishes_an_interrupted_copy(self):
        self.configure()
        write(self.dest() / "model-00001.safetensors", b"partial")  # what an interrupted rsync leaves
        j = self.run_one(self.mgr.add("Qwen-Test", meta=self.meta))
        self.assert_copied(j)

    def test_mac_unreachable(self):
        self.configure()
        os.environ["FAKE_MAC_DOWN"] = "1"
        j = self.run_one(self.mgr.add("Qwen-Test", meta=self.meta))
        self.assertEqual(j["status"], "failed")
        self.assertIn("can't reach chris@mac.local", j["error"])

    def test_connection_test(self):
        self.configure(mode="pull", mac_source=str(self.llm))
        r = self.mgr.test()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["home"], str(self.home))
        self.assertEqual(r["source"], "ok")

    def slow_rsync(self):
        """An rsync on the 'Mac' that takes a moment to start, so a test can act mid-copy."""
        w = self.tmp / "bin" / "slow-rsync"
        write(w, f"#!/bin/sh\nsleep 1.5\nexec {shutil.which('rsync')} \"$@\"\n".encode())
        w.chmod(0o755)
        return str(w)

    def wait_for(self, cond, timeout=15):
        end = time.time() + timeout
        while not cond():
            self.assertLess(time.time(), end, "timed out")
            time.sleep(0.05)

    def test_pull_cancel_stops_the_copy_on_the_mac(self):
        self.configure(mode="pull", mac_source=str(self.llm), rsync=self.slow_rsync())
        j = self.mgr.add("Qwen-Test", meta=self.meta)
        t = threading.Thread(target=self.mgr.run, args=(j,), daemon=True); t.start()
        self.wait_for(lambda: j["status"] == "running")
        self.mgr.cancel(j["id"])
        t.join(20)
        self.assertEqual(j["status"], "cancelled")
        jobdir = Path(j["remote_dir"])
        self.wait_for(lambda: (jobdir / "status").exists())
        self.assertEqual((jobdir / "status").read_text().strip(), "cancelled")

    def test_pull_reattaches_after_a_restart(self):
        self.configure(mode="pull", mac_source=str(self.llm), rsync=self.slow_rsync())
        j = self.mgr.add("Qwen-Test", meta=self.meta)
        t = threading.Thread(target=self.mgr.run, args=(j,), daemon=True); t.start()
        self.wait_for(lambda: j["status"] == "running")
        again = transfer.Manager(self.state, self.llm)  # the service restarted: the job is queued again
        j2 = again.jobs[0]
        self.assertEqual(j2["status"], "queued")
        self.run_one(j2, mgr=again)
        self.assertIn("re-attached", again.log_path(j2["id"]).read_text())
        self.assertEqual(j2["status"], "done", j2.get("error"))
        self.assertEqual((self.home / ".llm-manager" / "transfers" / j2["id"] / "run.sh").exists(), True)
        t.join(20)

    def test_listing_script(self):
        out = subprocess.run(["sh", "-c", f"D={self.llm / 'Qwen-Test'}\n" + transfer.LIST_SCRIPT],
                             capture_output=True, text=True).stdout
        got = transfer.parse_listing(out)
        self.assertEqual(got["model-00002.safetensors"], 200_000)
        self.assertEqual(got["sub/tokenizer.json"], 1000)


if __name__ == "__main__":
    unittest.main()
