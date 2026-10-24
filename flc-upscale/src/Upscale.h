/*
    Upscale

    Turns an 8-bit palettized image (a decoded FLIC frame, a picture bitmap, a screen page) into a larger 32-bit RGBA image and
    encodes it as a 32-bit PNG, using either ImageMagick (MagickWand) or OpenCV to do the resampling and the PNG encoding.

    Engine-independent: nothing here includes MoX or MoM headers, so it can link into ReMoM, HeMoM, or a standalone tool.

    Typical use, from a decoded ReMoM picture (16-byte header + column-major pixels) and the current 6-bit VGA palette:

        struct s_UPSCALE_OPTIONS options;
        struct s_UPSCALE_IMAGE image;
        Upscale_Default_Options(&options);
        options.backend = UPSCALE_BACKEND_OPENCV;
        options.filter = UPSCALE_FILTER_LANCZOS;
        options.scale = 4;
        if(Upscale_Bitmap(bitmap, current_palette, &options, &image) == UPSCALE_OK)
        {
            Upscale_Write_PNG_File(&image, "UNITS1_000.png");
            Upscale_Free_Image(&image);
        }
*/
#ifndef UPSCALE_H
#define UPSCALE_H

#include <stddef.h>
#include <stdint.h>



#ifdef __cplusplus
extern "C" {
#endif



enum e_UPSCALE_RESULT
{
    UPSCALE_OK              =  0,
    UPSCALE_ERR_ARGUMENT    = -1,   /* NULL pointer, zero size, bad enum value */
    UPSCALE_ERR_NO_BACKEND  = -2,   /* backend was not compiled in */
    UPSCALE_ERR_UNSUPPORTED = -3,   /* backend cannot do the requested filter (e.g. super-resolution on ImageMagick) */
    UPSCALE_ERR_MEMORY      = -4,
    UPSCALE_ERR_BACKEND     = -5,   /* the library itself reported an error; see Upscale_Last_Error() */
    UPSCALE_ERR_IO          = -6
};

enum e_UPSCALE_BACKEND
{
    UPSCALE_BACKEND_IMAGEMAGICK = 1,
    UPSCALE_BACKEND_OPENCV      = 2
};

enum e_UPSCALE_FILTER
{
    UPSCALE_FILTER_NEAREST  = 0,    /* hard pixels; exact for integer scales */
    UPSCALE_FILTER_BILINEAR = 1,
    UPSCALE_FILTER_BICUBIC  = 2,    /* Catmull-Rom on ImageMagick, INTER_CUBIC on OpenCV */
    UPSCALE_FILTER_LANCZOS  = 3,
    UPSCALE_FILTER_SUPERRES = 4     /* OpenCV dnn_superres only (EDSR/ESPCN/FSRCNN/LapSRN model file) */
};

/* Order of the pixels in the source buffer. */
enum e_UPSCALE_LAYOUT
{
    UPSCALE_LAYOUT_ROW_MAJOR    = 0,    /* pixel (x,y) at y * stride + x; the screen / video pages */
    UPSCALE_LAYOUT_COLUMN_MAJOR = 1     /* pixel (x,y) at x * stride + y; decoded pictures (pict_seg) */
};

/* What the 768 palette bytes hold. */
enum e_UPSCALE_PALETTE
{
    UPSCALE_PALETTE_VGA6        = 0,    /* 6-bit DAC values 0-63, stretched to 0-255 (63 -> 255) */
    UPSCALE_PALETTE_VGA6_SHIFT2 = 1,    /* 6-bit DAC values 0-63, shifted left by 2 (63 -> 252), same as the platform renderers */
    UPSCALE_PALETTE_RGB8        = 2     /* already 8-bit 0-255 */
};

#define UPSCALE_NO_TRANSPARENCY     (-1)
#define UPSCALE_MAX_SCALE           16
#define UPSCALE_BITMAP_HEADER_SIZE  16

struct s_UPSCALE_SOURCE
{
    const uint8_t * pixels;         /* width * height palette indices */
    int width;
    int height;
    int stride;                     /* bytes between rows (row-major) or columns (column-major); 0 means tightly packed */
    int layout;                     /* e_UPSCALE_LAYOUT */
    const uint8_t * palette;        /* 256 * 3 bytes, R G B */
    int palette_format;             /* e_UPSCALE_PALETTE */
    int transparent_index;          /* palette index written as alpha 0, or UPSCALE_NO_TRANSPARENCY */
};

struct s_UPSCALE_OPTIONS
{
    int backend;                    /* e_UPSCALE_BACKEND */
    int filter;                     /* e_UPSCALE_FILTER */
    int scale;                      /* 1 to UPSCALE_MAX_SCALE; used when dst_width / dst_height are 0 */
    int dst_width;                  /* 0 means width * scale */
    int dst_height;                 /* 0 means height * scale */
    int encode_png;                 /* nonzero: also fill s_UPSCALE_IMAGE.png */
    const char * superres_model_path;   /* UPSCALE_FILTER_SUPERRES: e.g. "ESPCN_x4.pb" */
    const char * superres_model_name;   /* UPSCALE_FILTER_SUPERRES: "edsr", "espcn", "fsrcnn" or "lapsrn" */
    int superres_model_scale;           /* UPSCALE_FILTER_SUPERRES: the factor the model was trained for; 0 means scale */
};

struct s_UPSCALE_IMAGE
{
    uint8_t * rgba;                 /* width * height * 4 bytes, R G B A, row-major, not premultiplied */
    int width;
    int height;
    uint8_t * png;                  /* 32-bit (8 bits per channel RGBA) PNG file contents, or NULL */
    size_t png_size;
    int backend;                    /* the backend that made it, and that encodes the PNG later if encode_png was 0 */
};



/* Optional: ImageMagick needs a global init, which happens on first use if this is not called. Call Upscale_Shutdown() at exit. */
int Upscale_Init(void);
void Upscale_Shutdown(void);

int Upscale_Is_Backend_Available(int backend);
int Upscale_Is_Filter_Supported(int backend, int filter);
const char * Upscale_Backend_Name(int backend);

void Upscale_Default_Options(struct s_UPSCALE_OPTIONS * options);

/* Palette expansion only, no resampling: writes width * height * 4 bytes of RGBA (row-major) to rgba. */
int Upscale_Indexed_To_RGBA(const struct s_UPSCALE_SOURCE * source, uint8_t * rgba);

/* The main entry point: expand, resample with options->backend, optionally encode PNG. Free the result with Upscale_Free_Image(). */
int Upscale_Image(const struct s_UPSCALE_SOURCE * source, const struct s_UPSCALE_OPTIONS * options, struct s_UPSCALE_IMAGE * result);

/*
    Convenience for a decoded ReMoM picture: a 16-byte header (width, height, ...) followed by column-major pixels, as produced by
    Draw_Picture_To_Bitmap() / Create_Picture(). palette is current_palette (6-bit VGA). Index 0 (ST_TRANSPARENT) becomes alpha 0.
*/
int Upscale_Bitmap(const uint8_t * bitmap, const uint8_t * palette, const struct s_UPSCALE_OPTIONS * options, struct s_UPSCALE_IMAGE * result);

/* Writes result->png to path; encodes it first if Upscale_Image() was run with encode_png = 0. */
int Upscale_Write_PNG_File(struct s_UPSCALE_IMAGE * image, const char * path);

/* Upscale_Image() + Upscale_Write_PNG_File() + Upscale_Free_Image(). */
int Upscale_Image_To_PNG_File(const struct s_UPSCALE_SOURCE * source, const struct s_UPSCALE_OPTIONS * options, const char * path);

void Upscale_Free_Image(struct s_UPSCALE_IMAGE * image);

/* Description of the last failure, with context. Not thread-safe: one shared buffer. */
const char * Upscale_Last_Error(void);



#ifdef __cplusplus
}
#endif

#endif /* UPSCALE_H */
