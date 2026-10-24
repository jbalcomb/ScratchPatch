/*
    Upscale - shared between Upscale.c and the backends. Not part of the public API.

    Backends receive straight (not premultiplied) RGBA, row-major, tightly packed. Transparent pixels are 0,0,0,0, which is also their
    premultiplied value, so a backend can resample the buffer as premultiplied and divide afterwards without a separate pass.
    Every buffer a backend hands back is allocated with malloc(), so Upscale_Free_Image() can free() it whatever made it.
*/
#ifndef UPSCALE_INTERNAL_H
#define UPSCALE_INTERNAL_H

#include "Upscale.h"



#ifdef __cplusplus
extern "C" {
#endif



#define UPSCALE_ERROR_SIZE 512

void Upscale_Set_Error(const char * format, ...);

int Upscale_ImageMagick_Init(void);
void Upscale_ImageMagick_Shutdown(void);
int Upscale_ImageMagick_Resize(const uint8_t * rgba, int width, int height, const struct s_UPSCALE_OPTIONS * options, int dst_width, int dst_height, struct s_UPSCALE_IMAGE * result);
int Upscale_ImageMagick_Encode_PNG(const uint8_t * rgba, int width, int height, uint8_t ** png, size_t * png_size);

int Upscale_OpenCV_Resize(const uint8_t * rgba, int width, int height, const struct s_UPSCALE_OPTIONS * options, int dst_width, int dst_height, struct s_UPSCALE_IMAGE * result);
int Upscale_OpenCV_Encode_PNG(const uint8_t * rgba, int width, int height, uint8_t ** png, size_t * png_size);
int Upscale_OpenCV_Has_Superres(void);



#ifdef __cplusplus
}
#endif

#endif /* UPSCALE_INTERNAL_H */
