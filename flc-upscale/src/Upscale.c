/*
    Upscale - palette expansion, argument checks, backend dispatch, PNG file output.
    See Upscale.h.
*/

#include "Upscale.h"
#include "Upscale_Internal.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>



static char upscale_last_error[UPSCALE_ERROR_SIZE] = "";



void Upscale_Set_Error(const char * format, ...)
{
    va_list args;
    va_start(args, format);
    vsnprintf(upscale_last_error, sizeof(upscale_last_error), format, args);
    va_end(args);
}

const char * Upscale_Last_Error(void)
{
    return upscale_last_error;
}


int Upscale_Init(void)
{
#ifdef UPSCALE_HAVE_IMAGEMAGICK
    return Upscale_ImageMagick_Init();
#else
    return UPSCALE_OK;
#endif
}

void Upscale_Shutdown(void)
{
#ifdef UPSCALE_HAVE_IMAGEMAGICK
    Upscale_ImageMagick_Shutdown();
#endif
}


int Upscale_Is_Backend_Available(int backend)
{
    switch(backend)
    {
#ifdef UPSCALE_HAVE_IMAGEMAGICK
        case UPSCALE_BACKEND_IMAGEMAGICK: { return 1; } break;
#endif
#ifdef UPSCALE_HAVE_OPENCV
        case UPSCALE_BACKEND_OPENCV:      { return 1; } break;
#endif
        default:                          { return 0; } break;
    }
}

int Upscale_Is_Filter_Supported(int backend, int filter)
{
    if(Upscale_Is_Backend_Available(backend) == 0)
    {
        return 0;
    }
    if((filter < UPSCALE_FILTER_NEAREST) || (filter > UPSCALE_FILTER_SUPERRES))
    {
        return 0;
    }
    if(filter == UPSCALE_FILTER_SUPERRES)
    {
#ifdef UPSCALE_HAVE_OPENCV
        return ((backend == UPSCALE_BACKEND_OPENCV) && (Upscale_OpenCV_Has_Superres() != 0));
#else
        return 0;
#endif
    }
    return 1;
}

const char * Upscale_Backend_Name(int backend)
{
    switch(backend)
    {
        case UPSCALE_BACKEND_IMAGEMAGICK: { return "ImageMagick"; } break;
        case UPSCALE_BACKEND_OPENCV:      { return "OpenCV";      } break;
        default:                          { return "unknown";     } break;
    }
}


void Upscale_Default_Options(struct s_UPSCALE_OPTIONS * options)
{
    if(options == NULL)
    {
        return;
    }
    memset(options, 0, sizeof(*options));
#ifdef UPSCALE_HAVE_IMAGEMAGICK
    options->backend = UPSCALE_BACKEND_IMAGEMAGICK;
#else
    options->backend = UPSCALE_BACKEND_OPENCV;
#endif
    options->filter = UPSCALE_FILTER_NEAREST;
    options->scale = 2;
    options->encode_png = 1;
}


static uint8_t Palette_Channel(uint8_t value, int palette_format)
{
    switch(palette_format)
    {
        case UPSCALE_PALETTE_VGA6:        { value &= 0x3F; return (uint8_t)((value << 2) | (value >> 4)); } break;
        case UPSCALE_PALETTE_VGA6_SHIFT2: { value &= 0x3F; return (uint8_t)(value << 2);                  } break;
        default:                          { return value;                                                 } break;
    }
}

static int Check_Source(const struct s_UPSCALE_SOURCE * source)
{
    int min_stride = 0;

    if((source == NULL) || (source->pixels == NULL) || (source->palette == NULL))
    {
        Upscale_Set_Error("Upscale: source, source->pixels and source->palette must not be NULL");
        return UPSCALE_ERR_ARGUMENT;
    }
    if((source->width <= 0) || (source->height <= 0))
    {
        Upscale_Set_Error("Upscale: bad source size %dx%d", source->width, source->height);
        return UPSCALE_ERR_ARGUMENT;
    }
    if((source->layout != UPSCALE_LAYOUT_ROW_MAJOR) && (source->layout != UPSCALE_LAYOUT_COLUMN_MAJOR))
    {
        Upscale_Set_Error("Upscale: bad source layout %d", source->layout);
        return UPSCALE_ERR_ARGUMENT;
    }
    if((source->palette_format < UPSCALE_PALETTE_VGA6) || (source->palette_format > UPSCALE_PALETTE_RGB8))
    {
        Upscale_Set_Error("Upscale: bad palette format %d", source->palette_format);
        return UPSCALE_ERR_ARGUMENT;
    }
    min_stride = (source->layout == UPSCALE_LAYOUT_ROW_MAJOR) ? source->width : source->height;
    if((source->stride != 0) && (source->stride < min_stride))
    {
        Upscale_Set_Error("Upscale: stride %d is smaller than the %s length %d", source->stride, (source->layout == UPSCALE_LAYOUT_ROW_MAJOR) ? "row" : "column", min_stride);
        return UPSCALE_ERR_ARGUMENT;
    }
    return UPSCALE_OK;
}


int Upscale_Indexed_To_RGBA(const struct s_UPSCALE_SOURCE * source, uint8_t * rgba)
{
    uint8_t lut[256][4];
    int stride = 0;
    int itr_x = 0;
    int itr_y = 0;
    int itr_color = 0;
    int status = 0;
    uint8_t color_index = 0;
    uint8_t * dst = NULL;

    status = Check_Source(source);
    if(status != UPSCALE_OK)
    {
        return status;
    }
    if(rgba == NULL)
    {
        Upscale_Set_Error("Upscale_Indexed_To_RGBA: rgba must not be NULL");
        return UPSCALE_ERR_ARGUMENT;
    }

    for(itr_color = 0; itr_color < 256; itr_color++)
    {
        lut[itr_color][0] = Palette_Channel(source->palette[(itr_color * 3) + 0], source->palette_format);
        lut[itr_color][1] = Palette_Channel(source->palette[(itr_color * 3) + 1], source->palette_format);
        lut[itr_color][2] = Palette_Channel(source->palette[(itr_color * 3) + 2], source->palette_format);
        lut[itr_color][3] = 0xFF;
    }
    if((source->transparent_index >= 0) && (source->transparent_index < 256))
    {
        /* 0,0,0,0 rather than the palette color: it is the premultiplied value, so filters do not bleed it into the edges */
        memset(lut[source->transparent_index], 0, 4);
    }

    if(source->layout == UPSCALE_LAYOUT_ROW_MAJOR)
    {
        stride = (source->stride != 0) ? source->stride : source->width;
    }
    else
    {
        stride = (source->stride != 0) ? source->stride : source->height;
    }

    dst = rgba;
    for(itr_y = 0; itr_y < source->height; itr_y++)
    {
        for(itr_x = 0; itr_x < source->width; itr_x++)
        {
            if(source->layout == UPSCALE_LAYOUT_ROW_MAJOR)
            {
                color_index = source->pixels[((size_t)itr_y * stride) + itr_x];
            }
            else
            {
                color_index = source->pixels[((size_t)itr_x * stride) + itr_y];
            }
            memcpy(dst, lut[color_index], 4);
            dst += 4;
        }
    }

    return UPSCALE_OK;
}


int Upscale_Image(const struct s_UPSCALE_SOURCE * source, const struct s_UPSCALE_OPTIONS * options, struct s_UPSCALE_IMAGE * result)
{
    uint8_t * rgba = NULL;
    int dst_width = 0;
    int dst_height = 0;
    int status = 0;

    if(result == NULL)
    {
        Upscale_Set_Error("Upscale_Image: result must not be NULL");
        return UPSCALE_ERR_ARGUMENT;
    }
    memset(result, 0, sizeof(*result));

    status = Check_Source(source);
    if(status != UPSCALE_OK)
    {
        return status;
    }
    if(options == NULL)
    {
        Upscale_Set_Error("Upscale_Image: options must not be NULL (see Upscale_Default_Options)");
        return UPSCALE_ERR_ARGUMENT;
    }
    if(Upscale_Is_Backend_Available(options->backend) == 0)
    {
        Upscale_Set_Error("Upscale_Image: backend %s (%d) was not compiled in", Upscale_Backend_Name(options->backend), options->backend);
        return UPSCALE_ERR_NO_BACKEND;
    }
    if((options->filter < UPSCALE_FILTER_NEAREST) || (options->filter > UPSCALE_FILTER_SUPERRES))
    {
        Upscale_Set_Error("Upscale_Image: bad filter %d", options->filter);
        return UPSCALE_ERR_ARGUMENT;
    }
    if(Upscale_Is_Filter_Supported(options->backend, options->filter) == 0)
    {
        Upscale_Set_Error("Upscale_Image: %s cannot do filter %d%s", Upscale_Backend_Name(options->backend), options->filter,
            (options->filter == UPSCALE_FILTER_SUPERRES) ? " (super-resolution needs OpenCV built with the dnn_superres contrib module)" : "");
        return UPSCALE_ERR_UNSUPPORTED;
    }
    if(((options->dst_width == 0) || (options->dst_height == 0)) && ((options->scale < 1) || (options->scale > UPSCALE_MAX_SCALE)))
    {
        Upscale_Set_Error("Upscale_Image: scale %d is outside 1 to %d", options->scale, UPSCALE_MAX_SCALE);
        return UPSCALE_ERR_ARGUMENT;
    }
    dst_width  = (options->dst_width  != 0) ? options->dst_width  : (source->width  * options->scale);
    dst_height = (options->dst_height != 0) ? options->dst_height : (source->height * options->scale);
    if((dst_width <= 0) || (dst_height <= 0) || (dst_width > (source->width * UPSCALE_MAX_SCALE)) || (dst_height > (source->height * UPSCALE_MAX_SCALE)))
    {
        Upscale_Set_Error("Upscale_Image: bad destination size %dx%d for a %dx%d source", dst_width, dst_height, source->width, source->height);
        return UPSCALE_ERR_ARGUMENT;
    }

    rgba = (uint8_t *)malloc((size_t)source->width * source->height * 4);
    if(rgba == NULL)
    {
        Upscale_Set_Error("Upscale_Image: out of memory expanding a %dx%d source", source->width, source->height);
        return UPSCALE_ERR_MEMORY;
    }
    Upscale_Indexed_To_RGBA(source, rgba);

    switch(options->backend)
    {
#ifdef UPSCALE_HAVE_IMAGEMAGICK
        case UPSCALE_BACKEND_IMAGEMAGICK: { status = Upscale_ImageMagick_Resize(rgba, source->width, source->height, options, dst_width, dst_height, result); } break;
#endif
#ifdef UPSCALE_HAVE_OPENCV
        case UPSCALE_BACKEND_OPENCV:      { status = Upscale_OpenCV_Resize(rgba, source->width, source->height, options, dst_width, dst_height, result);      } break;
#endif
        default:                          { status = UPSCALE_ERR_NO_BACKEND; } break;
    }

    free(rgba);

    if(status != UPSCALE_OK)
    {
        Upscale_Free_Image(result);
        return status;
    }
    result->backend = options->backend;
    return UPSCALE_OK;
}


int Upscale_Bitmap(const uint8_t * bitmap, const uint8_t * palette, const struct s_UPSCALE_OPTIONS * options, struct s_UPSCALE_IMAGE * result)
{
    struct s_UPSCALE_SOURCE source;

    if(bitmap == NULL)
    {
        Upscale_Set_Error("Upscale_Bitmap: bitmap must not be NULL");
        return UPSCALE_ERR_ARGUMENT;
    }

    memset(&source, 0, sizeof(source));
    source.width  = (int16_t)(bitmap[0] | (bitmap[1] << 8));
    source.height = (int16_t)(bitmap[2] | (bitmap[3] << 8));
    source.pixels = bitmap + UPSCALE_BITMAP_HEADER_SIZE;
    source.stride = 0;
    source.layout = UPSCALE_LAYOUT_COLUMN_MAJOR;
    source.palette = palette;
    source.palette_format = UPSCALE_PALETTE_VGA6;
    source.transparent_index = 0;  /* ST_TRANSPARENT */

    return Upscale_Image(&source, options, result);
}


int Upscale_Write_PNG_File(struct s_UPSCALE_IMAGE * image, const char * path)
{
    FILE * file = NULL;
    int status = UPSCALE_OK;

    if((image == NULL) || (image->rgba == NULL) || (path == NULL))
    {
        Upscale_Set_Error("Upscale_Write_PNG_File: image, image->rgba and path must not be NULL");
        return UPSCALE_ERR_ARGUMENT;
    }

    if(image->png == NULL)
    {
        switch(image->backend)
        {
#ifdef UPSCALE_HAVE_IMAGEMAGICK
            case UPSCALE_BACKEND_IMAGEMAGICK: { status = Upscale_ImageMagick_Encode_PNG(image->rgba, image->width, image->height, &image->png, &image->png_size); } break;
#endif
#ifdef UPSCALE_HAVE_OPENCV
            case UPSCALE_BACKEND_OPENCV:      { status = Upscale_OpenCV_Encode_PNG(image->rgba, image->width, image->height, &image->png, &image->png_size);      } break;
#endif
            default:
            {
                Upscale_Set_Error("Upscale_Write_PNG_File: image has no PNG and backend %d is not available to encode one", image->backend);
                status = UPSCALE_ERR_NO_BACKEND;
            } break;
        }
        if(status != UPSCALE_OK)
        {
            return status;
        }
    }

    file = fopen(path, "wb");
    if(file == NULL)
    {
        Upscale_Set_Error("Upscale_Write_PNG_File: cannot open \"%s\" for writing", path);
        return UPSCALE_ERR_IO;
    }
    if(fwrite(image->png, 1, image->png_size, file) != image->png_size)
    {
        Upscale_Set_Error("Upscale_Write_PNG_File: short write of %u bytes to \"%s\"", (unsigned)image->png_size, path);
        status = UPSCALE_ERR_IO;
    }
    if(fclose(file) != 0)
    {
        Upscale_Set_Error("Upscale_Write_PNG_File: error closing \"%s\"", path);
        status = UPSCALE_ERR_IO;
    }
    return status;
}


int Upscale_Image_To_PNG_File(const struct s_UPSCALE_SOURCE * source, const struct s_UPSCALE_OPTIONS * options, const char * path)
{
    struct s_UPSCALE_IMAGE image;
    int status = 0;

    status = Upscale_Image(source, options, &image);
    if(status != UPSCALE_OK)
    {
        return status;
    }
    status = Upscale_Write_PNG_File(&image, path);
    Upscale_Free_Image(&image);
    return status;
}


void Upscale_Free_Image(struct s_UPSCALE_IMAGE * image)
{
    if(image == NULL)
    {
        return;
    }
    free(image->rgba);
    free(image->png);
    memset(image, 0, sizeof(*image));
}
