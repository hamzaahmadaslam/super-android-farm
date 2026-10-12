// A static export: `next build` writes plain files to out/ and nothing runs on a server.
export default {
  output: 'export',
  trailingSlash: true,               // /docs/gpu/ is out/docs/gpu/index.html, and /panel/ needs its slash
  images: { unoptimized: true },
  reactStrictMode: true,
  poweredByHeader: false,
  turbopack: { root: import.meta.dirname },
};
