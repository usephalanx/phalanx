/**
 * Jest mock for static file imports (images, fonts, etc.).
 *
 * When a component imports a `.jpg`, `.png`, `.svg`, or other binary asset,
 * Jest resolves to this module and returns a predictable placeholder string
 * so the import succeeds without bundler-specific loaders.
 */
const fileMock = 'test-file-stub';
export default fileMock;
