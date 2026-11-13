// A link that changes the page without reloading it; modified clicks (new tab, new window) stay with the browser.
export function Link({ href, navigate, children, ...props }) {
  return <a href={href} onClick={(event) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
    event.preventDefault(); navigate(href)
  }} {...props}>{children}</a>
}
