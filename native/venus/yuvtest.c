/* Video/camera path: the CPU fills a YUV hardware buffer through gralloc,
 * the GPU samples it as an external texture, and the result is read back.
 *   yuvtest [w h format_hex]     format 0x23 = YCbCr_420_888, 0x32315659 = YV12 */
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

static GLuint shader(GLenum type, const char *src) {
   GLuint s = glCreateShader(type); glShaderSource(s, 1, &src, NULL); glCompileShader(s);
   GLint ok = 0; glGetShaderiv(s, GL_COMPILE_STATUS, &ok);
   if (!ok) { char log[512]; glGetShaderInfoLog(s, sizeof(log), NULL, log); printf("shader: %s\n", log); }
   return s;
}

int main(int argc, char **argv) {
   const int w = argc > 2 ? atoi(argv[1]) : 640, h = argc > 2 ? atoi(argv[2]) : 360;
   const uint32_t format = argc > 3 ? (uint32_t)strtoul(argv[3], NULL, 16) : 0x23;
   EGLDisplay dpy = eglGetDisplay(EGL_DEFAULT_DISPLAY);
   if (!eglInitialize(dpy, NULL, NULL)) return 1;
   const EGLint ca[] = { EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT, EGL_SURFACE_TYPE, EGL_PBUFFER_BIT,
                         EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8, EGL_ALPHA_SIZE, 8, EGL_NONE };
   EGLConfig cfg; EGLint n = 0; eglChooseConfig(dpy, ca, &cfg, 1, &n);
   const EGLint xa[] = { EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE };
   EGLContext ctx = eglCreateContext(dpy, cfg, EGL_NO_CONTEXT, xa);
   const EGLint pa[] = { EGL_WIDTH, 64, EGL_HEIGHT, 64, EGL_NONE };
   EGLSurface pb = eglCreatePbufferSurface(dpy, cfg, pa);
   if (!eglMakeCurrent(dpy, pb, pb, ctx)) { printf("makeCurrent failed 0x%x\n", eglGetError()); return 1; }

   AHardwareBuffer_Desc desc = { .width = w, .height = h, .layers = 1, .format = format,
      .usage = AHARDWAREBUFFER_USAGE_CPU_WRITE_OFTEN | AHARDWAREBUFFER_USAGE_CPU_READ_OFTEN | AHARDWAREBUFFER_USAGE_GPU_SAMPLED_IMAGE };
   AHardwareBuffer *ahb = NULL;
   int r = AHardwareBuffer_allocate(&desc, &ahb);
   printf("allocate %dx%d format 0x%x = %d\n", w, h, format, r);
   if (r) return 1;

   /* left half pure red, right half pure blue (BT.601 limited range) */
   AHardwareBuffer_Planes pl;
   r = AHardwareBuffer_lockPlanes(ahb, AHARDWAREBUFFER_USAGE_CPU_WRITE_OFTEN, -1, NULL, &pl);
   if (r || pl.planeCount < 3) { printf("lockPlanes failed: %d planes %u\n", r, r ? 0 : pl.planeCount); return 2; }
   printf("planes: Y stride %u, Cb pixelStride %u rowStride %u, Cr rowStride %u\n", pl.planes[0].rowStride,
          pl.planes[1].pixelStride, pl.planes[1].rowStride, pl.planes[2].rowStride);
   for (int y = 0; y < h; y++)
      for (int x = 0; x < w; x++)
         ((uint8_t *)pl.planes[0].data)[(size_t)y * pl.planes[0].rowStride + x] = x < w / 2 ? 81 : 41;
   for (int y = 0; y < (h + 1) / 2; y++)
      for (int x = 0; x < (w + 1) / 2; x++) {
         const int left = x < w / 4;
         ((uint8_t *)pl.planes[1].data)[(size_t)y * pl.planes[1].rowStride + (size_t)x * pl.planes[1].pixelStride] = left ? 90 : 240;
         ((uint8_t *)pl.planes[2].data)[(size_t)y * pl.planes[2].rowStride + (size_t)x * pl.planes[2].pixelStride] = left ? 240 : 110;
      }
   AHardwareBuffer_unlock(ahb, NULL);

   const EGLint ia[] = { EGL_IMAGE_PRESERVED_KHR, EGL_TRUE, EGL_NONE };
   EGLImageKHR img = eglCreateImageKHR(dpy, EGL_NO_CONTEXT, EGL_NATIVE_BUFFER_ANDROID, eglGetNativeClientBufferANDROID(ahb), ia);
   printf("eglCreateImageKHR = %p (error 0x%x)\n", (void *)img, eglGetError());
   if (img == EGL_NO_IMAGE_KHR) return 3;

   GLuint tex; glGenTextures(1, &tex);
   glBindTexture(GL_TEXTURE_EXTERNAL_OES, tex);
   glEGLImageTargetTexture2DOES(GL_TEXTURE_EXTERNAL_OES, (GLeglImageOES)img);
   glTexParameteri(GL_TEXTURE_EXTERNAL_OES, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
   glTexParameteri(GL_TEXTURE_EXTERNAL_OES, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
   printf("external texture bound, GL error 0x%x\n", glGetError());

   GLuint p = glCreateProgram();
   glAttachShader(p, shader(GL_VERTEX_SHADER, "attribute vec2 a; varying vec2 t; void main(){ t = a * 0.5 + 0.5; gl_Position = vec4(a, 0.0, 1.0); }"));
   glAttachShader(p, shader(GL_FRAGMENT_SHADER, "#extension GL_OES_EGL_image_external : require\nprecision mediump float; uniform samplerExternalOES s; varying vec2 t; void main(){ gl_FragColor = texture2D(s, t); }"));
   glBindAttribLocation(p, 0, "a"); glLinkProgram(p); glUseProgram(p);
   const float quad[] = { -1, -1, 1, -1, -1, 1, 1, 1 };
   glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, quad); glEnableVertexAttribArray(0);
   glViewport(0, 0, 64, 64);
   glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
   unsigned char l[4], rr[4];
   glReadPixels(8, 32, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, l);
   glReadPixels(56, 32, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, rr);
   printf("left  %3u %3u %3u (expect about 255 0 0)\nright %3u %3u %3u (expect about 0 0 255), GL error 0x%x\n", l[0], l[1], l[2], rr[0], rr[1], rr[2], glGetError());
   const int ok = l[0] > 200 && l[1] < 60 && l[2] < 60 && rr[0] < 60 && rr[1] < 60 && rr[2] > 200;
   printf("%s\n", ok ? "RESULT: PASS" : "RESULT: FAIL");
   return ok ? 0 : 4;
}
