"""quants.py: which files make up each quant of a multi-quant GGUF repo. Run: python -m unittest"""
import unittest

import quants

G = 10**9


def repo(*pairs):
    return {p: s for p, s in pairs}


# shaped like a real repo that mixes a patched model, a second model family, an imatrix and a re-made quant
MIXED = repo(
    (".gitattributes", 1), ("README.md", 1),
    ("mmproj-Model-BF16.gguf", G), ("mmproj-Other-BF16.gguf", G),
    ("patch/tool.py", 1), ("patch/all/Model-patch-all.gguf", 3 * G),
    ("other-IQ2_XS/Other-IQ2_XS-00001-of-00002.gguf", 40 * G), ("other-IQ2_XS/Other-IQ2_XS-00002-of-00002.gguf", 28 * G),
    ("unsloth-Q8_0/Model-Q8_0-00001-of-00002.gguf", 100 * G), ("unsloth-Q8_0/Model-Q8_0-00002-of-00002.gguf", 88 * G),
    ("unsloth-UD-Q4_K_XL/Model-UD-Q4_K_XL-00001-of-00002.gguf", 60 * G),
    ("unsloth-UD-Q4_K_XL/Model-UD-Q4_K_XL-00002-of-00002.gguf", 50 * G),
    ("unsloth-UD-Q4_K_XL-V2/Model-UD-Q4_K_XL-00001-of-00002.gguf", 60 * G),
    ("unsloth-UD-Q4_K_XL-V2/Model-UD-Q4_K_XL-00002-of-00002.gguf", 50 * G),
    ("unsloth-UD-Q4_K_XL-V2/imatrix_micro3.gguf", G),
)


class Parse(unittest.TestCase):
    def test_quant_from_file_name(self):
        self.assertEqual(quants.parse("Q4/Qwen3-8B-UD-Q4_K_XL-00001-of-00002.gguf"),
                         ("UD-Q4_K_XL", "qwen3-8b", "Qwen3-8B-UD-Q4_K_XL"))

    def test_fp16_is_f16(self):
        self.assertEqual(quants.quant_of("m-fp16.gguf"), "F16")

    def test_bits(self):
        self.assertEqual([quants.bits(q) for q in ("Q8_0", "UD-IQ4_XS", "BF16", "MXFP4_MOE")], [8, 4, 16, 4])


class Group(unittest.TestCase):
    def test_single_gguf_downloads_whole_repo(self):
        self.assertIsNone(quants.group(repo(("m-Q4_K_M.gguf", G), ("README.md", 1))))

    def test_mlx_downloads_whole_repo(self):
        self.assertIsNone(quants.group(repo(("model.safetensors", G), ("config.json", 1))))

    def test_q8_only_takes_its_shards_and_shared_files(self):
        sel = set(quants.select(MIXED, "Q8_0"))
        self.assertIn("unsloth-Q8_0/Model-Q8_0-00001-of-00002.gguf", sel)
        self.assertIn("unsloth-Q8_0/Model-Q8_0-00002-of-00002.gguf", sel)
        self.assertIn("README.md", sel)
        self.assertFalse(any("Q4_K_XL" in p or "IQ2_XS" in p or "patch-all" in p for p in sel))

    def test_imatrix_gguf_is_not_a_variant_or_download(self):
        g = quants.group(MIXED)
        self.assertFalse(any("imatrix" in k for k in g["variants"]))
        self.assertFalse(any("imatrix" in p for p in quants.select(MIXED, "Q8_0")))

    def test_same_quant_in_two_folders_stays_apart(self):
        g = quants.group(MIXED)
        self.assertIn("unsloth-UD-Q4_K_XL", g["variants"])
        self.assertIn("unsloth-UD-Q4_K_XL-V2", g["variants"])
        self.assertEqual(len(g["variants"]["unsloth-UD-Q4_K_XL-V2"]), 2)

    def test_describe_sizes_and_flags(self):
        d = {v["id"]: v for v in quants.describe(MIXED)["variants"]}
        self.assertEqual(d["Q8_0"]["size"], 188 * G + 2 * G + 3)  # shards + both projectors + 3 small files
        self.assertTrue(d["Q8_0"]["quant"])
        self.assertTrue(d["unsloth-UD-Q4_K_XL-V2"]["quant"])
        self.assertEqual(d["unsloth-UD-Q4_K_XL-V2"]["bits"], 4)
        self.assertFalse(d["Model-patch-all"]["quant"])

    def test_unknown_variant(self):
        with self.assertRaises(ValueError):
            quants.select(MIXED, "Q2_K")
        self.assertEqual(quants.select(MIXED, "Q2_K", strict=False), {})

    def test_default_prefers_4bit(self):
        self.assertEqual(quants.default({"Q8_0": [], "Q4_K_M": [], "Q6_K": []}), "Q4_K_M")


if __name__ == "__main__":
    unittest.main()
