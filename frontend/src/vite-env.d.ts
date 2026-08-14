/// <reference types="vite/client" />

// Brings in Vite's ambient module declarations, which is what makes
// `import "./styles.css"` legal to TypeScript.
//
// Without it a clean install fails with TS2882 ("Cannot find module or type
// declarations for side-effect import of './styles.css'"). It only showed up
// in CI: a developer machine that has built before carries enough resolved
// types in node_modules to let it pass, so `npm run typecheck` looked fine
// locally and broke on every clean checkout - including the image build.
