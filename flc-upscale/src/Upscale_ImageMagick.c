/*
    Upscale - ImageMagick backend (MagickWand C API, ImageMagick 6 or 7).

    ImageMagick weights by alpha when it resizes, so transparent pixels do not darken the edges of a sprite.
    Nearest uses MagickSampleImage(), which copies pixels and never blends.
*/

#include "Upscale_Internal.h"

#include <stdlib.h>
#include <string.h>

#if defined(UPSCALE_IMAGEMAGICK_7)
#include <MagickWand/MagickWand.h>
#else
#include <wand/MagickWand.h>
typedef FilterTypes FilterType;  /* ImageMagick 7 renamed the enum */
#endif



static int imagemagick_initialized = 0;



int Upscale_ImageMagick_Init(void)
{
    if(imagemagick_initialized == 0)
    {
        MagickWandGenesis();
        imagemagick_initialized = 1;
    }
    return UPSCALE_OK;
}

void Upscale_ImageMagick_Shutdown(void)
{
    if(imagemagick_initialized != 0)
    {
        MagickWandTerminus();
        imagemagick_initialized = 0;
    }
}


static int Wand_Error(MagickWand * wand, const char * context)
{
    ExceptionType severity;
    char * description = NULL;

    description = MagickGetException(wand, &severity);
    Upscale_Set_Error("ImageMagick: %s failed: %s (severity %d)", context, (description != NULL) ? description : "no description", (int)severity);
    if(description != NULL)
    {
        MagickRelinquishMemory(description);
    }
    return UPSCALE_ERR_BACKEND;
}

static FilterType Filter_Type(int filter)
{
    switch(filter)
    {
        case UPSCALE_FILTER_BILINEAR: { return TriangleFilter; } break;
        case UPSCALE_FILTER_BICUBIC:  { return CatromFilter;   } break;  /* CubicFilter is a blurry B-spline; Catmull-Rom is what other libraries call bicubic */
        case UPSCALE_FILTER_LANCZOS:  { return LanczosFilter;  } break;
        default:                      { return PointFilter;    } break;
    }
}

static MagickWand * Wand_From_RGBA(const uint8_t * rgba, int width, int height)
{
    MagickWand * wand = NULL;

    wand = NewMagickWand();
    if(wand == NULL)
    {
        Upscale_Set_Error("ImageMagick: NewMagickWand() returned NULL");
        return NULL;
    }
    if(MagickConstituteImage(wand, (size_t)width, (size_t)height, "RGBA", CharPixel, rgba) == MagickFalse)
    {
        Wand_Error(wand, "MagickConstituteImage");
        DestroyMagickWand(wand);
        return NULL;
    }
    return wand;
}

/* "PNG32" is ImageMagick's name for 8-bit RGBA PNG; without it, ImageMagick may write a smaller palette or opaque PNG. */
static int Wand_To_PNG(MagickWand * wand, uint8_t ** png, size_t * png_size)
{
    unsigned char * blob = NULL;
    size_t blob_size = 0;

    if(MagickSetImageFormat(wand, "PNG32") == MagickFalse)
    {
        return Wand_Error(wand, "MagickSetImageFormat(PNG32)");
    }
    if(MagickSetImageDepth(wand, 8) == MagickFalse)
    {
        return Wand_Error(wand, "MagickSetImageDepth(8)");
    }
    blob = MagickGetImageBlob(wand, &blob_size);
    if((blob == NULL) || (blob_size == 0))
    {
        if(blob != NULL)
        {
            MagickRelinquishMemory(blob);
        }
        return Wand_Error(wand, "MagickGetImageBlob");
    }

    *png = (uint8_t *)malloc(blob_size);
    if(*png == NULL)
    {
        MagickRelinquishMemory(blob);
        Upscale_Set_Error("ImageMagick: out of memory copying a %u byte PNG", (unsigned)blob_size);
        return UPSCALE_ERR_MEMORY;
    }
    memcpy(*png, blob, blob_size);
    *png_size = blob_size;
    MagickRelinquishMemory(blob);
    return UPSCALE_OK;
}


int Upscale_ImageMagick_Resize(const uint8_t * rgba, int width, int height, const struct s_UPSCALE_OPTIONS * options, int dst_width, int dst_height, struct s_UPSCALE_IMAGE * result)
{
    MagickWand * wand = NULL;
    MagickBooleanType ok = MagickFalse;
    int status = UPSCALE_OK;

    if(options->filter == UPSCALE_FILTER_SUPERRES)
    {
        Upscale_Set_Error("ImageMagick: super-resolution is not available; use UPSCALE_BACKEND_OPENCV");
        return UPSCALE_ERR_UNSUPPORTED;
    }

    Upscale_ImageMagick_Init();

    wand = Wand_From_RGBA(rgba, width, height);
    if(wand == NULL)
    {
        return UPSCALE_ERR_BACKEND;
    }

    if(options->filter == UPSCALE_FILTER_NEAREST)
    {
        ok = MagickSampleImage(wand, (size_t)dst_width, (size_t)dst_height);
    }
    else
    {
#if defined(UPSCALE_IMAGEMAGICK_7)
        ok = MagickResizeImage(wand, (size_t)dst_width, (size_t)dst_height, Filter_Type(options->filter));
#else
        ok = MagickResizeImage(wand, (size_t)dst_width, (size_t)dst_height, Filter_Type(options->filter), 1.0);
#endif
    }
    if(ok == MagickFalse)
    {
        status = Wand_Error(wand, (options->filter == UPSCALE_FILTER_NEAREST) ? "MagickSampleImage" : "MagickResizeImage");
        DestroyMagickWand(wand);
        return status;
    }

    result->width = dst_width;
    result->height = dst_height;
    result->rgba = (uint8_t *)malloc((size_t)dst_width * dst_height * 4);
    if(result->rgba == NULL)
    {
        DestroyMagickWand(wand);
        Upscale_Set_Error("ImageMagick: out of memory for a %dx%d result", dst_width, dst_height);
        return UPSCALE_ERR_MEMORY;
    }
    if(MagickExportImagePixels(wand, 0, 0, (size_t)dst_width, (size_t)dst_height, "RGBA", CharPixel, result->rgba) == MagickFalse)
    {
        status = Wand_Error(wand, "MagickExportImagePixels");
        DestroyMagickWand(wand);
        return status;
    }

    if(options->encode_png != 0)
    {
        status = Wand_To_PNG(wand, &result->png, &result->png_size);
    }

    DestroyMagickWand(wand);
    return status;
}


int Upscale_ImageMagick_Encode_PNG(const uint8_t * rgba, int width, int height, uint8_t ** png, size_t * png_size)
{
    MagickWand * wand = NULL;
    int status = UPSCALE_OK;

    Upscale_ImageMagick_Init();

    wand = Wand_From_RGBA(rgba, width, height);
    if(wand == NULL)
    {
        return UPSCALE_ERR_BACKEND;
    }
    status = Wand_To_PNG(wand, png, png_size);
    DestroyMagickWand(wand);
    return status;
}
