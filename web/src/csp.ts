/** The content security policy of spec 14.7, verbatim. The build injects it as a meta tag,
 * deploy/nginx.conf sends it as a header, and the Playwright CSP check compares against it. */
export const STRICT_CSP =
  "default-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'";

/** What the build's meta tag carries: the same policy without `frame-ancestors`, which browsers
 * ignore in a meta element (and log a notice about). The header in deploy/nginx.conf carries the
 * full policy; X-Frame-Options DENY backs it up. */
export const META_CSP = STRICT_CSP.replace(/;\s*frame-ancestors 'none'/, '');
