/**
 * Jest mock for CSS / style imports.
 *
 * When a component imports a `.css`, `.less`, `.scss`, or `.sass` file,
 * Jest resolves to this module and returns an empty object so the import
 * succeeds without attempting to parse actual stylesheets.
 */
const styleMock: Record<string, string> = {};
export default styleMock;
