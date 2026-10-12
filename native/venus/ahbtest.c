/* Reproduce SurfaceFlinger's buffer import outside SurfaceFlinger, with EGL's
 * debug callback on so ANGLE says why it refuses.  ahbtest [w h usage_hex] */
#define EGL_EGLEXT_PROTOTYPES
#define GL_GLEXT_PROTOTYPES
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES2/gl2.h>
#include <GLES2/gl2ext.h>
#include <android/hardware_buffer.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void
debug_cb(EGLenum error, const char *command, EGLint type, EGLLabelKHR t, EGLLabelKHR o, const char *message)
{
   (void)type; (void)t; (void)o;
   printf("EGL debug: 0x%x in %s: %s\n", error, command, message ? message : "(null)");
}

int
main(int argc, char **argv)
{
   const int w = argc > 2 ? atoi(argv[1]) : 480, h = argc > 2 ? atoi(argv[2]) : 854;
   const uint64_t usage = argc > 3 ? strtoull(argv[3], NULL, 16) : 0xb00;

   const EGLAttrib dbg[] = { EGL_DEBUG_MSG_ERROR_KHR, EGL_TRUE, EGL_DEBUG_MSG_WARN_KHR, EGL_TRUE, EGL_NONE };
   PFNEGLDEBUGMESSAGECONTROLKHRPROC ctl = (PFNEGLDEBUGMESSAGECONTROLKHRPROC)eglGetProcAddress("eglDebugMessageControlKHR");
   if (ctl)
      ctl(debug_cb, dbg);

   EGLDisplay dpy = eglGetDisplay(EGL_DEFAULT_DISPLAY);
   if (!eglInitialize(dpy, NULL, NULL)) { printf("eglInitialize failed 0x%x\n", eglGetError()); return 1; }
   const EGLint cfg_attr[] = { EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT, EGL_SURFACE_TYPE, EGL_PBUFFER_BIT, EGL_NONE };
   EGLConfig cfg; EGLint n = 0;
   eglChooseConfig(dpy, cfg_attr, &cfg, 1, &n);
   const EGLint ctx_attr[] = { EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE };
   EGLContext ctx = eglCreateContext(dpy, cfg, EGL_NO_CONTEXT, ctx_attr);
   const EGLint pb_attr[] = { EGL_WIDTH, 16, EGL_HEIGHT, 16, EGL_NONE };
   EGLSurface pb = eglCreatePbufferSurface(dpy, cfg, pb_attr);
   if (!eglMakeCurrent(dpy, pb, pb, ctx)) { printf("eglMakeCurrent failed 0x%x\n", eglGetError()); return 1; }
   printf("GL_RENDERER: %s\n", glGetString(GL_RENDERER));

   AHardwareBuffer_Desc desc = { .width = w, .height = h, .layers = 1,
                                 .format = AHARDWAREBUFFER_FORMAT_R8G8B8A8_UNORM, .usage = usage };
   AHardwareBuffer *ahb = NULL;
   int r = AHardwareBuffer_allocate(&desc, &ahb);
   printf("AHardwareBuffer_allocate(%dx%d usage 0x%llx) = %d\n", w, h, (unsigned long long)usage, r);
   if (r) return 1;

   EGLClientBuffer cb = eglGetNativeClientBufferANDROID(ahb);
   const EGLint img_attr[] = { EGL_IMAGE_PRESERVED_KHR, EGL_TRUE, EGL_NONE };
   EGLImageKHR img = eglCreateImageKHR(dpy, EGL_NO_CONTEXT, EGL_NATIVE_BUFFER_ANDROID, cb, img_attr);
   printf("eglCreateImageKHR = %p (error 0x%x)\n", (void *)img, eglGetError());
   if (img == EGL_NO_IMAGE_KHR) return 2;

   /* draw into it and read back, as the compositor would */
   GLuint tex, fbo;
   glGenTextures(1, &tex);
   glBindTexture(GL_TEXTURE_2D, tex);
   glEGLImageTargetTexture2DOES(GL_TEXTURE_2D, (GLeglImageOES)img);
   glGenFramebuffers(1, &fbo);
   glBindFramebuffer(GL_FRAMEBUFFER, fbo);
   glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, tex, 0);
   printf("framebuffer status 0x%x (complete is 0x8cd5), GL error 0x%x\n", glCheckFramebufferStatus(GL_FRAMEBUFFER), glGetError());
   /* a different colour in each quadrant plus a one-pixel marker, so a wrong
    * tile order shows up */
   glViewport(0, 0, w, h);
   glEnable(GL_SCISSOR_TEST);
   const int hw = w / 2, hh = h / 2;
   const struct { int x, y, cw, ch; float r, g, b; } q[] = {
      { 0, 0, hw, hh, 0x11 / 255.f, 0x66 / 255.f, 0xcc / 255.f },
      { hw, 0, w - hw, hh, 0xee / 255.f, 0x22 / 255.f, 0x33 / 255.f },
      { 0, hh, hw, h - hh, 0x44 / 255.f, 0xdd / 255.f, 0x55 / 255.f },
      { hw, hh, w - hw, h - hh, 0xff / 255.f, 0xaa / 255.f, 0x00 / 255.f },
      { 37, 301 % h, 1, 1, 1.f, 1.f, 1.f },
   };
   for (unsigned i = 0; i < sizeof(q) / sizeof(q[0]); i++) {
      glScissor(q[i].x, q[i].y, q[i].cw, q[i].ch);
      glClearColor(q[i].r, q[i].g, q[i].b, 1.f);
      glClear(GL_COLOR_BUFFER_BIT);
   }
   glDisable(GL_SCISSOR_TEST);
   const int pts[][2] = { { 0, 0 }, { w - 1, 0 }, { 0, h - 1 }, { w - 1, h - 1 }, { 37, 301 % h }, { 38, 301 % h },
                          { hw - 1, hh - 1 }, { hw, hh }, { 17, 9 }, { 63, 8 }, { 64, 7 } };
   const int npts = sizeof(pts) / sizeof(pts[0]);
   unsigned char gl[16][4];
   for (int i = 0; i < npts; i++)
      glReadPixels(pts[i][0], pts[i][1], 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, gl[i]);
   glFinish();
   printf("GL error after drawing: 0x%x\n", glGetError());

   /* what the CPU sees through gralloc lock() */
   AHardwareBuffer_Desc got;
   AHardwareBuffer_describe(ahb, &got);
   void *p = NULL;
   r = AHardwareBuffer_lock(ahb, AHARDWAREBUFFER_USAGE_CPU_READ_OFTEN, -1, NULL, &p);
   if (r || !p) { printf("lock() failed: %d\n", r); return 3; }
   int bad = 0;
   for (int i = 0; i < npts; i++) {
      const unsigned char *c = (const unsigned char *)p + ((size_t)pts[i][1] * got.stride + pts[i][0]) * 4;
      const int same = !memcmp(c, gl[i], 4);
      bad += !same;
      printf("(%3d,%3d) GPU %02x%02x%02x%02x  CPU %02x%02x%02x%02x  %s\n", pts[i][0], pts[i][1], gl[i][0], gl[i][1],
             gl[i][2], gl[i][3], c[0], c[1], c[2], c[3], same ? "ok" : "DIFFERENT");
   }
   /* CPU writes one pixel, GPU must see it */
   AHardwareBuffer_unlock(ahb, NULL);
   r = AHardwareBuffer_lock(ahb, AHARDWAREBUFFER_USAGE_CPU_WRITE_OFTEN | AHARDWAREBUFFER_USAGE_CPU_READ_OFTEN, -1, NULL, &p);
   if (r == 0 && p) {
      unsigned char *c = (unsigned char *)p + ((size_t)20 * got.stride + 100) * 4;
      c[0] = 0x12; c[1] = 0x34; c[2] = 0x56; c[3] = 0xff;
      AHardwareBuffer_unlock(ahb, NULL);
      unsigned char back[4] = { 0 };
      glReadPixels(100, 20, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, back);
      const int same = back[0] == 0x12 && back[1] == 0x34 && back[2] == 0x56;
      bad += !same;
      printf("CPU wrote 123456ff at (100,20), GPU reads %02x%02x%02x%02x  %s\n", back[0], back[1], back[2], back[3],
             same ? "ok" : "DIFFERENT");
   }
   printf("%s\n", bad ? "RESULT: FAIL" : "RESULT: PASS");
   return 0;
}
