import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from build import make_icons
from fnos_icon import mask_256
from test_archive import synthetic_stage
from verify import VerificationError, verify_stage


class IconTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stage = synthetic_stage(self.root / 'stage')
        self.source = self.root / 'source.png'
        Image.new('RGB', (1024, 1024), (30, 60, 90)).save(self.source)
        make_icons(self.source, self.stage)

    def test_official_template_mask_is_lossless(self):
        mask = mask_256()
        # Hash the official alpha pixels, not a Pillow/zlib-specific PNG encoding.
        self.assertEqual(hashlib.sha256(mask.tobytes()).hexdigest(),
                         '41114ed3abadcff5e6323f662954cb6c4e36c915dc48ef42d256da14de34443d')
        self.assertEqual(mask.size, (256, 256))
        self.assertEqual(mask.getpixel((0, 0)), 0)
        self.assertEqual(mask.getpixel((128, 128)), 255)
        with Image.open(self.stage / 'ICON_256.PNG') as image:
            self.assertEqual(image.getchannel('A').tobytes(), mask.tobytes())

    def test_both_sizes_and_desktop_icons_match(self):
        for size, name in [(64, 'ICON.PNG'), (256, 'ICON_256.PNG')]:
            root = self.stage / name
            desktop = self.stage / 'app/ui/images' / ('icon_' + str(size) + '.png')
            self.assertEqual(root.read_bytes(), desktop.read_bytes())
            with Image.open(root) as image:
                self.assertEqual(image.format, 'PNG')
                self.assertEqual(image.size, (size, size))
                self.assertEqual(image.mode, 'RGBA')
                self.assertEqual(image.getpixel((0, 0))[3], 0)
                self.assertEqual(image.getpixel((size // 2, size // 2)), (30, 60, 90, 255))
                self.assertEqual(image.info.get('srgb'), 0)
            self.assertLess(root.stat().st_size, 1024 * 1024)

    def test_rounded_icons_pass_stage_verification(self):
        verify_stage(self.stage)

    def test_transparent_center_is_rejected(self):
        path = self.stage / 'ICON_256.PNG'
        with Image.open(path) as image:
            image.putpixel((128, 128), (255, 255, 255, 0))
            image.save(path)
        with self.assertRaisesRegex(VerificationError, 'Transparent app icon center'):
            verify_stage(self.stage)

    def test_transparent_source_gets_opaque_interior(self):
        Image.new('RGBA', (1024, 1024), (0, 0, 0, 0)).save(self.source)
        make_icons(self.source, self.stage)
        with Image.open(self.stage / 'ICON_256.PNG') as image:
            self.assertEqual(image.getpixel((128, 128)), (255, 255, 255, 255))
            self.assertEqual(image.getchannel('A').tobytes(), mask_256().tobytes())
        verify_stage(self.stage)


if __name__ == '__main__':
    unittest.main()
