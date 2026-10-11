// Development-only tool; requires sharp (not an application dependency).
// Run: NODE_PATH=/path/to/node_modules node scripts/generate-icons.cjs
const fs = require('node:fs/promises');
const path = require('node:path');
const sharp = require('sharp');

const iconDir = path.join(__dirname, '..', 'app', 'static', 'icons');

async function generate() {
  const source = await fs.readFile(path.join(iconDir, 'icon.svg'), 'utf8');
  // iOS supplies its own corners; maskable icons need an opaque, full-bleed tile.
  const square = source.replace(' rx="28"', '');
  // Keep the complete mark inside Android's central 80%-diameter safe circle.
  const maskable = square.replace(
    '<g id="mark">',
    '<g id="mark" transform="translate(64 64) scale(0.82) translate(-68 -56)">',
  );
  const png = (svg, size) => sharp(Buffer.from(svg), { density: 144 })
    .resize(size, size)
    .png()
    .toBuffer();
  const writePng = async (name, svg, size) => {
    await fs.writeFile(path.join(iconDir, name), await png(svg, size));
  };

  await writePng('favicon-16x16.png', source, 16);
  await writePng('favicon-32x32.png', source, 32);
  for (const size of [120, 152, 167, 180]) {
    const name = size === 180 ? 'apple-touch-icon.png' : `apple-touch-icon-${size}x${size}.png`;
    await writePng(name, square, size);
  }
  for (const size of [192, 512]) {
    await writePng(`android-chrome-${size}x${size}.png`, source, size);
    await writePng(`android-maskable-${size}x${size}.png`, maskable, size);
  }

  // ICO directory with independent PNG frames for each browser fallback size.
  const sizes = [16, 32, 48];
  const frames = await Promise.all(sizes.map(size => png(source, size)));
  const directory = Buffer.alloc(6 + 16 * frames.length);
  directory.writeUInt16LE(1, 2);
  directory.writeUInt16LE(frames.length, 4);
  let offset = directory.length;
  frames.forEach((frame, index) => {
    const entry = 6 + 16 * index;
    directory[entry] = directory[entry + 1] = sizes[index];
    directory.writeUInt16LE(1, entry + 4);
    directory.writeUInt16LE(32, entry + 6);
    directory.writeUInt32LE(frame.length, entry + 8);
    directory.writeUInt32LE(offset, entry + 12);
    offset += frame.length;
  });
  await fs.writeFile(path.join(iconDir, 'favicon.ico'), Buffer.concat([directory, ...frames]));
  console.log('Generated browser, iOS and Android icons from icon.svg.');
}

generate().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
