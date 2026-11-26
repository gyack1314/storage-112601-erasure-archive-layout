"""2+1 XOR 编解码与补零、重建路径测试。"""

import unittest

from cold_archive import coding
from cold_archive.errors import BadPiece


class CodingTests(unittest.TestCase):
    def test_even_length_round_trip(self):
        source = bytes.fromhex("10203040")
        pieces = coding.encode(source)
        self.assertEqual(pieces[0], bytes.fromhex("1020"))
        self.assertEqual(pieces[1], bytes.fromhex("3040"))
        self.assertEqual(
            pieces[2],
            bytes(a ^ b for a, b in zip(pieces[0], pieces[1], strict=True)),
        )
        for pair in ({0: pieces[0], 1: pieces[1]},
                     {0: pieces[0], 2: pieces[2]},
                     {1: pieces[1], 2: pieces[2]}):
            self.assertEqual(coding.rebuild(pair, len(source)), source)

    def test_odd_length_uses_padding_and_trims_on_rebuild(self):
        source = bytes.fromhex("a1b2c3d4e5")
        pieces = coding.encode(source)
        self.assertEqual(len(pieces[0]), 3)
        self.assertEqual(len(pieces[1]), 3)  # 末字节补零
        rebuilt = coding.rebuild({0: pieces[0], 2: pieces[2]}, len(source))
        self.assertEqual(rebuilt, source)

    def test_single_index_cannot_rebuild(self):
        pieces = coding.encode(b"abcd")
        with self.assertRaises(BadPiece):
            coding.rebuild({2: pieces[2]}, 4)

    def test_empty_content(self):
        pieces = coding.encode(b"")
        self.assertEqual(pieces, [b"", b"", b""])
        self.assertEqual(coding.rebuild({0: b"", 1: b""}, 0), b"")


if __name__ == "__main__":
    unittest.main()
