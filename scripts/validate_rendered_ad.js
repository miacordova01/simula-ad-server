// Validate a rendered ad by PARSING its <script> blocks with a real JS engine.
//
// This is the strongest available check that the template escaping worked: if a
// quote, backslash, newline or </script> leaked through unescaped, V8 refuses
// to parse the block. It also round-trips the declared constants so you can see
// the value the browser would actually receive.
//
//   node scripts/validate_rendered_ad.js samples/scifi_feed.html
//   node scripts/validate_rendered_ad.js /tmp/adv/*.html

const fs = require("fs");
const vm = require("vm");

const NAMES = [
  "CHAR_NAME", "CAMPAIGN", "CHAR_MESSAGE", "CTA",
  "TRACKING_URL", "IMPRESSION_URL", "AD_ID", "API_URL", "THEME",
];

function validate(file, { verbose = true } = {}) {
  const html = fs.readFileSync(file, "utf8");
  const blocks = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script\s*>/gi)]
    .map((m) => m[1]);

  let ok = true;
  const name = file.split("/").pop();

  if (blocks.length !== 2) {
    ok = false;
    console.log(`  ${name}: expected 2 script blocks, found ${blocks.length}`);
  }

  blocks.forEach((src, i) => {
    try {
      new vm.Script(src);
    } catch (e) {
      ok = false;
      console.log(`  ${name}: block ${i + 1} PARSE ERROR -> ${e.message}`);
    }
  });

  // `const` in a vm.Script is script-scoped and does NOT become a property of
  // the context object, so read the bindings back with an appended expression
  // evaluated in the same scope.
  let values = {};
  try {
    const ctx = { __out: {} };
    vm.createContext(ctx);
    const reader = NAMES.map((k) => `try{__out[${JSON.stringify(k)}]=${k}}catch(e){}`).join(";");
    new vm.Script(blocks[0] + "\n;" + reader).runInContext(ctx);
    values = ctx.__out;
  } catch (e) {
    ok = false;
    console.log(`  ${name}: could not read constants -> ${e.message}`);
  }

  if (verbose) {
    console.log(`  ${name}  (${blocks.length} script blocks, all parse: ${ok})`);
    for (const k of NAMES) {
      if (values[k] === undefined) continue;
      const v = JSON.stringify(values[k]);
      console.log(`      ${k.padEnd(15)} ${v.length > 78 ? v.slice(0, 75) + '..."' : v}`);
    }
  }
  return ok;
}

const files = process.argv.slice(2);
if (files.length === 0) {
  console.error("usage: node scripts/validate_rendered_ad.js <file.html> [...]");
  process.exit(2);
}
let allOk = true;
for (const f of files) allOk = validate(f, { verbose: files.length <= 3 }) && allOk;
if (files.length > 3) console.log(`  ${files.length} files checked, all valid: ${allOk}`);
process.exit(allOk ? 0 : 1);
