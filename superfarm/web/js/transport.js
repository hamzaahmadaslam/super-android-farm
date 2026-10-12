// The panel's door to the daemon. Every request, every socket and every address the
// browser loads by itself (a picture, a download) is made here and nowhere else, so
// that the network is named in one small module.
//
// Here the four functions pass straight through to the browser. The public demo swaps
// this one file for a backend that runs inside the page (demo/mock/ in the repository);
// that is how the same panel runs there with no daemon behind it.

// fetch(), unchanged. api.js builds the request (same origin, the CSRF header).
export const request = (path, init) => fetch(path, init);

// A WebSocket to the daemon, on the origin the page came from.
export function openSocket(path) {
  return new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}${path}`);
}

// The address for an <img src> or a download link.
export const mediaUrl = (path) => path;

// Show a multipart JPEG stream in `img`. Returns the function that ends it: clearing
// src is what closes the request; removing the element alone does not.
export function streamInto(img, path) {
  img.src = path;
  return () => { img.src = ''; };
}
