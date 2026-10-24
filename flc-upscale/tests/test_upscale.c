/*
    test_upscale - checks every compiled-in backend against a small synthetic ReMoM-style picture.

    usage: test_upscale [output_dir]     writes the upscaled PNGs there for a visual check

    The picture is 12x10, column-major, with a 16-byte header, like a decoded pict_seg:
        x 0-5,  y 0-6   color 5   (red,   VGA 63,0,0)
        x 0-5,  y 7-9   color 9   (green, VGA 0,40,0)
        x 11,   y 9     color 200 (blue,  VGA 0,0,63)    a single off-center pixel, so a transposed layout is caught
        elsewhere       color 0   (ST_TRANSPARENT)
*/

#include "Upscale.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>



#define TEST_WIDTH  12
#define TEST_HEIGHT 10
#define TEST_SCALE   4

static int test_failures = 0;

#define CHECK(_condition_, ...)                                             \
    do                                                                      \
    {                                                                       \
        if(!(_condition_))                                                  \
        {                                                                   \
            test_failures++;                                                \
            fprintf(stderr, "FAIL %s:%d: ", __FILE__, __LINE__);            \
            fprintf(stderr, __VA_ARGS__);                                   \
            fprintf(stderr, "\n");                                          \
        }                                                                   \
    } while(0)



static uint8_t test_palette[768];
static uint8_t test_bitmap[UPSCALE_BITMAP_HEADER_SIZE + (TEST_WIDTH * TEST_HEIGHT)];
static uint8_t test_row_major[TEST_WIDTH * TEST_HEIGHT];

static const char * filter_names[] = { "nearest", "bilinear", "bicubic", "lanczos", "superres", "area" };



static uint8_t Test_Color_Index(int x, int y)
{
    if((x == 11) && (y == 9))
    {
        return 200;
    }
    if(x < 6)
    {
        return (y < 7) ? 5 : 9;
    }
    return 0;
}

static void Build_Test_Picture(void)
{
    int itr_x = 0;
    int itr_y = 0;

    memset(test_palette, 0, sizeof(test_palette));
    test_palette[(5 * 3) + 0] = 63;
    test_palette[(9 * 3) + 1] = 40;
    test_palette[(200 * 3) + 2] = 63;
    test_palette[(0 * 3) + 0] = 33;     /* the transparent color has a palette entry too; it must not show up */

    memset(test_bitmap, 0, sizeof(test_bitmap));
    test_bitmap[0] = TEST_WIDTH;
    test_bitmap[2] = TEST_HEIGHT;
    test_bitmap[4] = 0x0A;              /* e_FLIC_Decoded 0xDE0A, as Create_Picture() writes it */
    test_bitmap[5] = 0xDE;
    for(itr_x = 0; itr_x < TEST_WIDTH; itr_x++)
    {
        for(itr_y = 0; itr_y < TEST_HEIGHT; itr_y++)
        {
            test_bitmap[UPSCALE_BITMAP_HEADER_SIZE + (itr_x * TEST_HEIGHT) + itr_y] = Test_Color_Index(itr_x, itr_y);
            test_row_major[(itr_y * TEST_WIDTH) + itr_x] = Test_Color_Index(itr_x, itr_y);
        }
    }
}

/* Expected straight RGBA of a source pixel, with UPSCALE_PALETTE_VGA6 and index 0 transparent. */
static void Expected_RGBA(int x, int y, uint8_t * rgba)
{
    uint8_t color_index = Test_Color_Index(x, y);
    int itr_channel = 0;
    uint8_t value = 0;

    if(color_index == 0)
    {
        memset(rgba, 0, 4);
        return;
    }
    for(itr_channel = 0; itr_channel < 3; itr_channel++)
    {
        value = test_palette[(color_index * 3) + itr_channel];
        rgba[itr_channel] = (uint8_t)((value << 2) | (value >> 4));
    }
    rgba[3] = 255;
}

static uint32_t Get_U32_BE(const uint8_t * buffer)
{
    return ((uint32_t)buffer[0] << 24) | ((uint32_t)buffer[1] << 16) | ((uint32_t)buffer[2] << 8) | (uint32_t)buffer[3];
}

static void Check_PNG(const struct s_UPSCALE_IMAGE * image, const char * label)
{
    static const uint8_t png_signature[8] = { 0x89, 'P', 'N', 'G', 0x0D, 0x0A, 0x1A, 0x0A };

    CHECK(image->png != NULL, "%s: no PNG", label);
    if(image->png == NULL)
    {
        return;
    }
    CHECK(image->png_size > 33, "%s: PNG is only %u bytes", label, (unsigned)image->png_size);
    CHECK(memcmp(image->png, png_signature, 8) == 0, "%s: bad PNG signature", label);
    CHECK(memcmp(image->png + 12, "IHDR", 4) == 0, "%s: first chunk is not IHDR", label);
    CHECK(Get_U32_BE(image->png + 16) == (uint32_t)image->width, "%s: PNG width %u, image width %d", label, Get_U32_BE(image->png + 16), image->width);
    CHECK(Get_U32_BE(image->png + 20) == (uint32_t)image->height, "%s: PNG height %u, image height %d", label, Get_U32_BE(image->png + 20), image->height);
    CHECK(image->png[24] == 8, "%s: PNG bit depth %d, expected 8", label, image->png[24]);
    CHECK(image->png[25] == 6, "%s: PNG color type %d, expected 6 (RGBA)", label, image->png[25]);
}

static void Check_Nearest(const struct s_UPSCALE_IMAGE * image, const char * label)
{
    uint8_t expected[4];
    const uint8_t * actual = NULL;
    int itr_x = 0;
    int itr_y = 0;
    int mismatches = 0;

    for(itr_y = 0; itr_y < image->height; itr_y++)
    {
        for(itr_x = 0; itr_x < image->width; itr_x++)
        {
            Expected_RGBA(itr_x / TEST_SCALE, itr_y / TEST_SCALE, expected);
            actual = image->rgba + (((size_t)itr_y * image->width) + itr_x) * 4;
            if(memcmp(actual, expected, 4) != 0)
            {
                if(mismatches == 0)
                {
                    fprintf(stderr, "%s: first mismatch at (%d,%d): got %d,%d,%d,%d expected %d,%d,%d,%d\n", label, itr_x, itr_y,
                        actual[0], actual[1], actual[2], actual[3], expected[0], expected[1], expected[2], expected[3]);
                }
                mismatches++;
            }
        }
    }
    CHECK(mismatches == 0, "%s: %d pixels differ from the source scaled by %d", label, mismatches, TEST_SCALE);
}

/* Interior of the red block stays red; its semi-transparent edge stays red too (not darkened by the transparent pixels' black). */
static void Check_Smooth(const struct s_UPSCALE_IMAGE * image, const char * label)
{
    const uint8_t * pixel = NULL;
    int itr_x = 0;
    int itr_y = 0;
    int edge_pixels = 0;
    int dark_edges = 0;

    pixel = image->rgba + (((size_t)(2 * TEST_SCALE) * image->width) + (2 * TEST_SCALE)) * 4;
    CHECK((pixel[0] >= 250) && (pixel[1] <= 5) && (pixel[2] <= 5) && (pixel[3] >= 250), "%s: red interior is %d,%d,%d,%d", label, pixel[0], pixel[1], pixel[2], pixel[3]);

    pixel = image->rgba + (((size_t)(2 * TEST_SCALE) * image->width) + (9 * TEST_SCALE)) * 4;
    CHECK(pixel[3] <= 5, "%s: transparent interior has alpha %d", label, pixel[3]);

    for(itr_y = 0; itr_y < (3 * TEST_SCALE); itr_y++)
    {
        for(itr_x = (4 * TEST_SCALE); itr_x < (9 * TEST_SCALE); itr_x++)
        {
            pixel = image->rgba + (((size_t)itr_y * image->width) + itr_x) * 4;
            if((pixel[3] >= 64) && (pixel[3] <= 250))
            {
                edge_pixels++;
                if((pixel[0] < 200) || (pixel[1] > 40) || (pixel[2] > 40))
                {
                    dark_edges++;
                }
            }
        }
    }
    CHECK(edge_pixels > 0, "%s: no semi-transparent edge pixels; the filter did not blend", label);
    CHECK(dark_edges == 0, "%s: %d of %d edge pixels are not red; alpha was not handled", label, dark_edges, edge_pixels);
}

static void Test_Palette_Expansion(void)
{
    struct s_UPSCALE_SOURCE source;
    uint8_t from_columns[TEST_WIDTH * TEST_HEIGHT * 4];
    uint8_t from_rows[TEST_WIDTH * TEST_HEIGHT * 4];
    uint8_t * red = NULL;

    memset(&source, 0, sizeof(source));
    source.pixels = test_bitmap + UPSCALE_BITMAP_HEADER_SIZE;
    source.width = TEST_WIDTH;
    source.height = TEST_HEIGHT;
    source.layout = UPSCALE_LAYOUT_COLUMN_MAJOR;
    source.palette = test_palette;
    source.palette_format = UPSCALE_PALETTE_VGA6;
    source.transparent_index = 0;
    CHECK(Upscale_Indexed_To_RGBA(&source, from_columns) == UPSCALE_OK, "expand column-major: %s", Upscale_Last_Error());

    source.pixels = test_row_major;
    source.layout = UPSCALE_LAYOUT_ROW_MAJOR;
    CHECK(Upscale_Indexed_To_RGBA(&source, from_rows) == UPSCALE_OK, "expand row-major: %s", Upscale_Last_Error());
    CHECK(memcmp(from_columns, from_rows, sizeof(from_rows)) == 0, "column-major and row-major sources expand differently");

    red = from_rows;  /* (0,0) */
    CHECK((red[0] == 255) && (red[3] == 255), "VGA6: 63 expanded to %d (alpha %d), expected 255", red[0], red[3]);
    CHECK(from_rows[(6 * 4) + 3] == 0 && from_rows[(6 * 4) + 0] == 0, "transparent pixel is %d,%d,%d,%d", from_rows[24], from_rows[25], from_rows[26], from_rows[27]);

    source.palette_format = UPSCALE_PALETTE_VGA6_SHIFT2;
    source.transparent_index = UPSCALE_NO_TRANSPARENCY;
    CHECK(Upscale_Indexed_To_RGBA(&source, from_rows) == UPSCALE_OK, "expand shift2: %s", Upscale_Last_Error());
    CHECK(from_rows[0] == 252, "VGA6_SHIFT2: 63 expanded to %d, expected 252", from_rows[0]);
    CHECK((from_rows[(6 * 4) + 0] == (33 << 2)) && (from_rows[(6 * 4) + 3] == 255), "no transparency: index 0 should be its opaque palette color");

    source.stride = TEST_WIDTH - 1;
    CHECK(Upscale_Indexed_To_RGBA(&source, from_rows) == UPSCALE_ERR_ARGUMENT, "a stride shorter than a row was accepted");
}

static void Check_Size(int width, int height, int scale, int aspect_width, int aspect_height, int dst_width, int dst_height, int expected_width, int expected_height)
{
    struct s_UPSCALE_OPTIONS options;
    int out_width = 0;
    int out_height = 0;
    int status = 0;

    Upscale_Default_Options(&options);
    options.scale = scale;
    options.aspect_width = aspect_width;
    options.aspect_height = aspect_height;
    options.dst_width = dst_width;
    options.dst_height = dst_height;
    status = Upscale_Destination_Size(width, height, &options, &out_width, &out_height);
    if(expected_width == 0)
    {
        CHECK(status == UPSCALE_ERR_ARGUMENT, "size %dx%d scale %d aspect %d:%d: expected an error, got %dx%d", width, height, scale, aspect_width, aspect_height, out_width, out_height);
        return;
    }
    CHECK((status == UPSCALE_OK) && (out_width == expected_width) && (out_height == expected_height), "size %dx%d scale %d aspect %d:%d dst %dx%d: got %dx%d (status %d), expected %dx%d",
        width, height, scale, aspect_width, aspect_height, dst_width, dst_height, out_width, out_height, status, expected_width, expected_height);
}

static void Test_Destination_Size(void)
{
    Check_Size(320, 200, 4, 0, 0, 0, 0, 1280, 800);     /* no aspect */
    Check_Size(320, 200, 4, 4, 3, 0, 0, 1280, 960);     /* 4:3 is taller than 16:10: width x4, height x4.8 */
    Check_Size(320, 200, 3, 8, 5, 0, 0, 960, 600);      /* the source's own shape */
    Check_Size(320, 200, 2, 16, 9, 0, 0, 711, 400);     /* 16:9 is wider: height x2, width x2.22 */
    Check_Size(100, 100, 2, 4, 3, 0, 0, 267, 200);
    Check_Size(320, 200, 4, 4, 3, 1000, 0, 1000, 750);  /* one side given: the other follows the aspect */
    Check_Size(320, 200, 4, 4, 3, 0, 900, 1200, 900);
    Check_Size(320, 200, 4, 4, 0, 0, 0, 0, 0);          /* half an aspect is an error */
    Check_Size(320, 200, 16, 1, 1, 0, 0, 0, 0);         /* height would be x25.6, past UPSCALE_MAX_SCALE */
}

/*
    Aspect 1:1 on the 12x10 picture at scale 4: width x4 exactly (48), height x4.8 (48), reached by copying rows x5 (50) and shrinking.
    Across, every output pixel must be an exact copy of its source pixel: groups of 4 identical columns and a hard red|transparent edge.
    Only the vertical direction may blend.
*/
static void Test_Aspect_Oversample(int backend, int filter, const char * output_dir)
{
    struct s_UPSCALE_OPTIONS options;
    struct s_UPSCALE_IMAGE image;
    uint8_t expected[4];
    const uint8_t * row = NULL;
    char label[64];
    char path[1024];
    int itr_x = 0;
    int itr_y = 0;
    int column_mismatches = 0;
    int soft_edges = 0;
    int status = 0;

    snprintf(label, sizeof(label), "%s/aspect-1:1-oversample-%s", Upscale_Backend_Name(backend), filter_names[filter]);
    Upscale_Default_Options(&options);
    options.backend = backend;
    options.filter = filter;
    options.scale = TEST_SCALE;
    options.aspect_width = 1;
    options.aspect_height = 1;
    options.oversample = UPSCALE_OVERSAMPLE_INTEGER;

    status = Upscale_Bitmap(test_bitmap, test_palette, &options, &image);
    CHECK(status == UPSCALE_OK, "%s: returned %d: %s", label, status, Upscale_Last_Error());
    if(status != UPSCALE_OK)
    {
        return;
    }
    CHECK((image.width == 48) && (image.height == 48), "%s: size %dx%d, expected 48x48", label, image.width, image.height);
    Check_PNG(&image, label);

    Expected_RGBA(5, 0, expected);
    for(itr_y = 0; itr_y < image.height; itr_y++)
    {
        row = image.rgba + ((size_t)itr_y * image.width * 4);
        for(itr_x = 0; itr_x < image.width; itr_x++)
        {
            if(memcmp(row + (itr_x * 4), row + ((itr_x / TEST_SCALE) * TEST_SCALE * 4), 4) != 0)
            {
                column_mismatches++;
            }
        }
        /* rows 0-30 come only from red source rows (0-6 copied x5 = 0-34, shrunk by 50/48) */
        if((itr_y <= 30) && ((memcmp(row + (23 * 4), expected, 4) != 0) || (row[(24 * 4) + 3] != 0)))
        {
            soft_edges++;
        }
    }
    CHECK(column_mismatches == 0, "%s: %d pixels differ from the first column of their group; the exact axis was resampled", label, column_mismatches);
    CHECK(soft_edges == 0, "%s: %d rows have a blended red|transparent edge at x 23|24", label, soft_edges);

    if(output_dir != NULL)
    {
        snprintf(path, sizeof(path), "%s/test_%s_aspect_1x1_oversample_%s.png", output_dir, Upscale_Backend_Name(backend), filter_names[filter]);
        CHECK(Upscale_Write_PNG_File(&image, path) == UPSCALE_OK, "%s: write %s: %s", label, path, Upscale_Last_Error());
    }
    Upscale_Free_Image(&image);
    printf("ok   %s\n", label);
}

static void Test_Backend(int backend, const char * output_dir)
{
    struct s_UPSCALE_OPTIONS options;
    struct s_UPSCALE_IMAGE image;
    char label[64];
    char path[1024];
    int filter = 0;
    int status = 0;

    if(Upscale_Is_Backend_Available(backend) == 0)
    {
        printf("skip %s: not compiled in\n", Upscale_Backend_Name(backend));
        return;
    }

    for(filter = UPSCALE_FILTER_NEAREST; filter <= UPSCALE_FILTER_LANCZOS; filter++)
    {
        snprintf(label, sizeof(label), "%s/%s", Upscale_Backend_Name(backend), filter_names[filter]);

        Upscale_Default_Options(&options);
        options.backend = backend;
        options.filter = filter;
        options.scale = TEST_SCALE;

        status = Upscale_Bitmap(test_bitmap, test_palette, &options, &image);
        CHECK(status == UPSCALE_OK, "%s: Upscale_Bitmap returned %d: %s", label, status, Upscale_Last_Error());
        if(status != UPSCALE_OK)
        {
            continue;
        }
        CHECK((image.width == TEST_WIDTH * TEST_SCALE) && (image.height == TEST_HEIGHT * TEST_SCALE), "%s: size %dx%d", label, image.width, image.height);
        Check_PNG(&image, label);
        if(filter == UPSCALE_FILTER_NEAREST)
        {
            Check_Nearest(&image, label);
        }
        else
        {
            Check_Smooth(&image, label);
        }

        if(output_dir != NULL)
        {
            snprintf(path, sizeof(path), "%s/test_%s_%s_x%d.png", output_dir, Upscale_Backend_Name(backend), filter_names[filter], TEST_SCALE);
            CHECK(Upscale_Write_PNG_File(&image, path) == UPSCALE_OK, "%s: write %s: %s", label, path, Upscale_Last_Error());
        }
        Upscale_Free_Image(&image);
        printf("ok   %s\n", label);
    }

    /* PNG encoded later, on write, and an explicit non-integer destination size */
    snprintf(label, sizeof(label), "%s/deferred-png", Upscale_Backend_Name(backend));
    Upscale_Default_Options(&options);
    options.backend = backend;
    options.filter = UPSCALE_FILTER_BICUBIC;
    options.encode_png = 0;
    options.dst_width = 30;
    options.dst_height = 25;
    status = Upscale_Bitmap(test_bitmap, test_palette, &options, &image);
    CHECK(status == UPSCALE_OK, "%s: returned %d: %s", label, status, Upscale_Last_Error());
    if(status == UPSCALE_OK)
    {
        CHECK((image.width == 30) && (image.height == 25) && (image.png == NULL), "%s: size %dx%d png %p", label, image.width, image.height, (void *)image.png);
        if(output_dir != NULL)
        {
            snprintf(path, sizeof(path), "%s/test_%s_bicubic_30x25.png", output_dir, Upscale_Backend_Name(backend));
            CHECK(Upscale_Write_PNG_File(&image, path) == UPSCALE_OK, "%s: write %s: %s", label, path, Upscale_Last_Error());
            Check_PNG(&image, label);
        }
        Upscale_Free_Image(&image);
        printf("ok   %s\n", label);
    }

    Test_Aspect_Oversample(backend, UPSCALE_FILTER_AREA, output_dir);
    Test_Aspect_Oversample(backend, UPSCALE_FILTER_BILINEAR, output_dir);

    /* oversample with whole-number factors on both axes leaves nothing to filter: the result is exactly nearest */
    snprintf(label, sizeof(label), "%s/oversample-integer", Upscale_Backend_Name(backend));
    Upscale_Default_Options(&options);
    options.backend = backend;
    options.filter = UPSCALE_FILTER_LANCZOS;
    options.scale = TEST_SCALE;
    options.oversample = UPSCALE_OVERSAMPLE_INTEGER;
    status = Upscale_Bitmap(test_bitmap, test_palette, &options, &image);
    CHECK(status == UPSCALE_OK, "%s: returned %d: %s", label, status, Upscale_Last_Error());
    if(status == UPSCALE_OK)
    {
        Check_Nearest(&image, label);
        Upscale_Free_Image(&image);
        printf("ok   %s\n", label);
    }

    /* UPSCALE_SUPERRES_MODEL=path/ESPCN_x4.pb (and optionally UPSCALE_SUPERRES_NAME=espcn) runs a real model */
    if((Upscale_Is_Filter_Supported(backend, UPSCALE_FILTER_SUPERRES) != 0) && (getenv("UPSCALE_SUPERRES_MODEL") != NULL))
    {
        snprintf(label, sizeof(label), "%s/superres", Upscale_Backend_Name(backend));
        Upscale_Default_Options(&options);
        options.backend = backend;
        options.filter = UPSCALE_FILTER_SUPERRES;
        options.scale = TEST_SCALE;
        options.superres_model_path = getenv("UPSCALE_SUPERRES_MODEL");
        options.superres_model_name = (getenv("UPSCALE_SUPERRES_NAME") != NULL) ? getenv("UPSCALE_SUPERRES_NAME") : "espcn";
        status = Upscale_Bitmap(test_bitmap, test_palette, &options, &image);
        CHECK(status == UPSCALE_OK, "%s: returned %d: %s", label, status, Upscale_Last_Error());
        if(status == UPSCALE_OK)
        {
            CHECK((image.width == TEST_WIDTH * TEST_SCALE) && (image.height == TEST_HEIGHT * TEST_SCALE), "%s: size %dx%d", label, image.width, image.height);
            Check_PNG(&image, label);
            Check_Smooth(&image, label);
            if(output_dir != NULL)
            {
                snprintf(path, sizeof(path), "%s/test_%s_superres_x%d.png", output_dir, Upscale_Backend_Name(backend), TEST_SCALE);
                CHECK(Upscale_Write_PNG_File(&image, path) == UPSCALE_OK, "%s: write %s: %s", label, path, Upscale_Last_Error());
            }
            Upscale_Free_Image(&image);
            printf("ok   %s\n", label);
        }
    }
    else if(Upscale_Is_Filter_Supported(backend, UPSCALE_FILTER_SUPERRES) == 0)
    {
        options.filter = UPSCALE_FILTER_SUPERRES;
        status = Upscale_Bitmap(test_bitmap, test_palette, &options, &image);
        CHECK(status == UPSCALE_ERR_UNSUPPORTED, "%s/superres: expected UPSCALE_ERR_UNSUPPORTED, got %d", Upscale_Backend_Name(backend), status);
        printf("ok   %s/superres reports unsupported\n", Upscale_Backend_Name(backend));
    }
}

int main(int argc, char ** argv)
{
    const char * output_dir = (argc > 1) ? argv[1] : NULL;
    struct s_UPSCALE_OPTIONS options;
    struct s_UPSCALE_IMAGE image;

    Build_Test_Picture();
    Upscale_Init();

    Test_Palette_Expansion();
    printf("ok   palette expansion and layouts\n");
    Test_Destination_Size();
    printf("ok   destination sizes\n");

    Test_Backend(UPSCALE_BACKEND_IMAGEMAGICK, output_dir);
    Test_Backend(UPSCALE_BACKEND_OPENCV, output_dir);

    Upscale_Default_Options(&options);
    options.backend = 99;
    CHECK(Upscale_Bitmap(test_bitmap, test_palette, &options, &image) == UPSCALE_ERR_NO_BACKEND, "unknown backend was accepted");
    options.backend = UPSCALE_BACKEND_OPENCV;
    options.scale = 0;
    if(Upscale_Is_Backend_Available(options.backend) != 0)
    {
        CHECK(Upscale_Bitmap(test_bitmap, test_palette, &options, &image) == UPSCALE_ERR_ARGUMENT, "scale 0 was accepted");
    }

    Upscale_Shutdown();

    if(test_failures != 0)
    {
        fprintf(stderr, "%d check(s) failed\n", test_failures);
        return 1;
    }
    printf("all checks passed\n");
    return 0;
}
