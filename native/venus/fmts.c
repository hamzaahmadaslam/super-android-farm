/* List which DRM modifiers the host offers for the formats Android video and
 * camera buffers use, and whether they can be exported/imported as dma-bufs. */
#include <stdio.h>
#include <vulkan/vulkan.h>
int main(void) {
   const VkApplicationInfo app = { .sType = VK_STRUCTURE_TYPE_APPLICATION_INFO, .apiVersion = VK_API_VERSION_1_2 };
   const VkInstanceCreateInfo ici = { .sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, .pApplicationInfo = &app };
   VkInstance inst; if (vkCreateInstance(&ici, NULL, &inst)) return 1;
   uint32_t n = 1; VkPhysicalDevice pd; vkEnumeratePhysicalDevices(inst, &n, &pd);
   VkPhysicalDeviceMemoryProperties mp; vkGetPhysicalDeviceMemoryProperties(pd, &mp);
   for (uint32_t i = 0; i < mp.memoryTypeCount; i++)
      printf("memory type %u: flags 0x%x heap %u\n", i, mp.memoryTypes[i].propertyFlags, mp.memoryTypes[i].heapIndex);
   const struct { VkFormat f; const char *name; } fmts[] = {
      { VK_FORMAT_G8_B8_R8_3PLANE_420_UNORM, "3-plane 420 (YV12/I420)" },
      { VK_FORMAT_G8_B8R8_2PLANE_420_UNORM, "2-plane 420 (NV12)" },
      { VK_FORMAT_R8_UNORM, "R8" }, { VK_FORMAT_R8G8_UNORM, "R8G8" },
      { VK_FORMAT_R8G8B8_UNORM, "RGB888" }, { VK_FORMAT_R5G6B5_UNORM_PACK16, "RGB565" },
      { VK_FORMAT_R16G16B16A16_SFLOAT, "RGBA FP16" }, { VK_FORMAT_A2B10G10R10_UNORM_PACK32, "RGBA1010102" },
      { VK_FORMAT_B8G8R8A8_UNORM, "BGRA8888" },
   };
   for (unsigned k = 0; k < sizeof(fmts) / sizeof(fmts[0]); k++) {
      VkDrmFormatModifierPropertiesEXT mods[64];
      VkDrmFormatModifierPropertiesListEXT ml = { .sType = VK_STRUCTURE_TYPE_DRM_FORMAT_MODIFIER_PROPERTIES_LIST_EXT,
                                                  .drmFormatModifierCount = 64, .pDrmFormatModifierProperties = mods };
      VkFormatProperties2 fp = { .sType = VK_STRUCTURE_TYPE_FORMAT_PROPERTIES_2, .pNext = &ml };
      vkGetPhysicalDeviceFormatProperties2(pd, fmts[k].f, &fp);
      printf("%-26s optimal 0x%x linear 0x%x modifiers %u:", fmts[k].name, fp.formatProperties.optimalTilingFeatures,
             fp.formatProperties.linearTilingFeatures, ml.drmFormatModifierCount);
      for (uint32_t i = 0; i < ml.drmFormatModifierCount; i++) {
         const VkPhysicalDeviceImageDrmFormatModifierInfoEXT mi = { .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_IMAGE_DRM_FORMAT_MODIFIER_INFO_EXT,
            .drmFormatModifier = mods[i].drmFormatModifier, .sharingMode = VK_SHARING_MODE_EXCLUSIVE };
         const VkPhysicalDeviceExternalImageFormatInfo ei = { .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_IMAGE_FORMAT_INFO,
            .pNext = &mi, .handleType = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT };
         const VkPhysicalDeviceImageFormatInfo2 ii = { .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_IMAGE_FORMAT_INFO_2, .pNext = &ei,
            .format = fmts[k].f, .type = VK_IMAGE_TYPE_2D, .tiling = VK_IMAGE_TILING_DRM_FORMAT_MODIFIER_EXT, .usage = VK_IMAGE_USAGE_SAMPLED_BIT };
         VkExternalImageFormatProperties ep = { .sType = VK_STRUCTURE_TYPE_EXTERNAL_IMAGE_FORMAT_PROPERTIES };
         VkImageFormatProperties2 ip = { .sType = VK_STRUCTURE_TYPE_IMAGE_FORMAT_PROPERTIES_2, .pNext = &ep };
         VkResult r = vkGetPhysicalDeviceImageFormatProperties2(pd, &ii, &ip);
         printf(" [%s%x planes %u feat 0x%x %s]", mods[i].drmFormatModifier ? "tiled .." : "LINEAR ", (unsigned)(mods[i].drmFormatModifier & 0xff),
                mods[i].drmFormatModifierPlaneCount, mods[i].drmFormatModifierTilingFeatures,
                r ? "no-sampled-dmabuf" : (ep.externalMemoryProperties.externalMemoryFeatures & 6) == 6 ? "export+import" : "partial");
      }
      printf("\n");
   }
   return 0;
}
