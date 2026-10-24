/*
    Upscale - OpenCV backend.

    OpenCV 4 has no C API, so this file is C++ with a C interface (see Upscale_Internal.h). No exception crosses that interface.

    cv::resize() does not know about alpha. The input is already premultiplied (transparent pixels are 0,0,0,0 and the rest are opaque),
    so resizing it as-is gives a premultiplied result, and Unpremultiply() turns that back into straight RGBA. Without that, sprite edges
    come out darkened by the black of the transparent pixels.

    UPSCALE_FILTER_SUPERRES runs a dnn_superres model (opencv_contrib) on the color channels; alpha is resized with INTER_CUBIC.
*/

#include "Upscale_Internal.h"

#include <cstdlib>
#include <cstring>
#include <vector>

#include <opencv2/core.hpp>
#include <opencv2/imgcodecs.hpp>
#include <opencv2/imgproc.hpp>

#if defined(UPSCALE_HAVE_OPENCV_SUPERRES)
#include <opencv2/dnn_superres.hpp>
#endif



static int Interpolation(int filter)
{
    switch(filter)
    {
        case UPSCALE_FILTER_BILINEAR: { return cv::INTER_LINEAR;   } break;
        case UPSCALE_FILTER_BICUBIC:  { return cv::INTER_CUBIC;    } break;
        case UPSCALE_FILTER_LANCZOS:  { return cv::INTER_LANCZOS4; } break;
        default:                      { return cv::INTER_NEAREST;  } break;
    }
}

/* Ringing (cubic, Lanczos) can push a color above its alpha; clamp it there before dividing. */
static void Unpremultiply(cv::Mat & rgba)
{
    int itr_x = 0;
    int itr_y = 0;
    int itr_channel = 0;
    unsigned int alpha = 0;
    unsigned int value = 0;
    uint8_t * pixel = NULL;

    for(itr_y = 0; itr_y < rgba.rows; itr_y++)
    {
        pixel = rgba.ptr<uint8_t>(itr_y);
        for(itr_x = 0; itr_x < rgba.cols; itr_x++, pixel += 4)
        {
            alpha = pixel[3];
            if(alpha == 0)
            {
                pixel[0] = 0;
                pixel[1] = 0;
                pixel[2] = 0;
            }
            else if(alpha < 255)
            {
                for(itr_channel = 0; itr_channel < 3; itr_channel++)
                {
                    value = pixel[itr_channel];
                    if(value > alpha)
                    {
                        value = alpha;
                    }
                    pixel[itr_channel] = (uint8_t)(((value * 255) + (alpha / 2)) / alpha);
                }
            }
        }
    }
}

static int Encode_PNG(const cv::Mat & rgba, uint8_t ** png, size_t * png_size)
{
    cv::Mat bgra;
    std::vector<uchar> buffer;

    cv::cvtColor(rgba, bgra, cv::COLOR_RGBA2BGRA);  /* imencode() expects OpenCV's BGRA order; a 4-channel Mat is written as an RGBA PNG */
    if(cv::imencode(".png", bgra, buffer) == false)
    {
        Upscale_Set_Error("OpenCV: imencode(.png) failed for a %dx%d image", rgba.cols, rgba.rows);
        return UPSCALE_ERR_BACKEND;
    }

    *png = (uint8_t *)std::malloc(buffer.size());
    if(*png == NULL)
    {
        Upscale_Set_Error("OpenCV: out of memory copying a %u byte PNG", (unsigned)buffer.size());
        return UPSCALE_ERR_MEMORY;
    }
    std::memcpy(*png, buffer.data(), buffer.size());
    *png_size = buffer.size();
    return UPSCALE_OK;
}

#if defined(UPSCALE_HAVE_OPENCV_SUPERRES)
/*
    The model only sees color, so the black behind transparent pixels would bleed into the sprite as a dark fringe.
    Grow the edge colors outward into the transparent area first: each pass gives every transparent pixel next to a colored one the average
    of its colored neighbors. Alpha is left alone, so the grown colors stay invisible.
*/
#define SUPERRES_BLEED_PASSES 8
static void Bleed_Edge_Colors(cv::Mat & rgba)
{
    static const int neighbor_x[4] = { -1, 1, 0, 0 };
    static const int neighbor_y[4] = { 0, 0, -1, 1 };
    cv::Mat filled(rgba.rows, rgba.cols, CV_8UC1);
    cv::Mat next_filled;
    cv::Mat next;
    int itr_pass = 0;
    int itr_x = 0;
    int itr_y = 0;
    int itr_neighbor = 0;
    int sample_x = 0;
    int sample_y = 0;
    int count = 0;
    int sum[3] = { 0, 0, 0 };
    int changed = 0;
    const uint8_t * sample = NULL;
    uint8_t * pixel = NULL;

    for(itr_y = 0; itr_y < rgba.rows; itr_y++)
    {
        for(itr_x = 0; itr_x < rgba.cols; itr_x++)
        {
            filled.at<uint8_t>(itr_y, itr_x) = (rgba.at<cv::Vec4b>(itr_y, itr_x)[3] != 0) ? 1 : 0;
        }
    }

    for(itr_pass = 0; itr_pass < SUPERRES_BLEED_PASSES; itr_pass++)
    {
        next = rgba.clone();
        next_filled = filled.clone();
        changed = 0;
        for(itr_y = 0; itr_y < rgba.rows; itr_y++)
        {
            for(itr_x = 0; itr_x < rgba.cols; itr_x++)
            {
                if(filled.at<uint8_t>(itr_y, itr_x) != 0)
                {
                    continue;
                }
                count = 0;
                sum[0] = 0;
                sum[1] = 0;
                sum[2] = 0;
                for(itr_neighbor = 0; itr_neighbor < 4; itr_neighbor++)
                {
                    sample_x = itr_x + neighbor_x[itr_neighbor];
                    sample_y = itr_y + neighbor_y[itr_neighbor];
                    if((sample_x < 0) || (sample_y < 0) || (sample_x >= rgba.cols) || (sample_y >= rgba.rows) || (filled.at<uint8_t>(sample_y, sample_x) == 0))
                    {
                        continue;
                    }
                    sample = rgba.ptr<uint8_t>(sample_y) + (sample_x * 4);
                    sum[0] += sample[0];
                    sum[1] += sample[1];
                    sum[2] += sample[2];
                    count++;
                }
                if(count != 0)
                {
                    pixel = next.ptr<uint8_t>(itr_y) + (itr_x * 4);
                    pixel[0] = (uint8_t)(sum[0] / count);
                    pixel[1] = (uint8_t)(sum[1] / count);
                    pixel[2] = (uint8_t)(sum[2] / count);
                    next_filled.at<uint8_t>(itr_y, itr_x) = 1;
                    changed = 1;
                }
            }
        }
        rgba = next;
        filled = next_filled;
        if(changed == 0)
        {
            break;
        }
    }
}

static int Superres(const cv::Mat & src, const struct s_UPSCALE_OPTIONS * options, int dst_width, int dst_height, cv::Mat & dst)
{
    cv::dnn_superres::DnnSuperResImpl model;
    cv::Mat channels[4];
    cv::Mat bgr;
    cv::Mat bgr_up;
    cv::Mat alpha_up;
    int model_scale = 0;

    if((options->superres_model_path == NULL) || (options->superres_model_name == NULL))
    {
        Upscale_Set_Error("OpenCV: UPSCALE_FILTER_SUPERRES needs superres_model_path and superres_model_name");
        return UPSCALE_ERR_ARGUMENT;
    }
    model_scale = (options->superres_model_scale != 0) ? options->superres_model_scale : options->scale;

    model.readModel(options->superres_model_path);
    model.setModel(options->superres_model_name, model_scale);

    cv::Mat bled = src.clone();
    Bleed_Edge_Colors(bled);
    cv::split(bled, channels);
    cv::Mat bgr_planes[3] = { channels[2], channels[1], channels[0] };
    cv::merge(bgr_planes, 3, bgr);
    model.upsample(bgr, bgr_up);
    if((bgr_up.cols != dst_width) || (bgr_up.rows != dst_height))
    {
        cv::resize(bgr_up, bgr_up, cv::Size(dst_width, dst_height), 0, 0, cv::INTER_AREA);
    }
    cv::resize(channels[3], alpha_up, cv::Size(dst_width, dst_height), 0, 0, cv::INTER_CUBIC);

    cv::split(bgr_up, bgr_planes);
    cv::Mat rgba_planes[4] = { bgr_planes[2], bgr_planes[1], bgr_planes[0], alpha_up };
    cv::merge(rgba_planes, 4, dst);
    return UPSCALE_OK;
}
#endif


extern "C" int Upscale_OpenCV_Has_Superres(void)
{
#if defined(UPSCALE_HAVE_OPENCV_SUPERRES)
    return 1;
#else
    return 0;
#endif
}


extern "C" int Upscale_OpenCV_Resize(const uint8_t * rgba, int width, int height, const struct s_UPSCALE_OPTIONS * options, int dst_width, int dst_height, struct s_UPSCALE_IMAGE * result)
{
    int status = UPSCALE_OK;

    try
    {
        const cv::Mat src(height, width, CV_8UC4, const_cast<uint8_t *>(rgba));
        cv::Mat dst;

        if(options->filter == UPSCALE_FILTER_SUPERRES)
        {
#if defined(UPSCALE_HAVE_OPENCV_SUPERRES)
            status = Superres(src, options, dst_width, dst_height, dst);
            if(status != UPSCALE_OK)
            {
                return status;
            }
#else
            Upscale_Set_Error("OpenCV: this build has no dnn_superres module (opencv_contrib)");
            return UPSCALE_ERR_UNSUPPORTED;
#endif
        }
        else
        {
            cv::resize(src, dst, cv::Size(dst_width, dst_height), 0, 0, Interpolation(options->filter));
            if(options->filter != UPSCALE_FILTER_NEAREST)
            {
                Unpremultiply(dst);
            }
        }

        result->width = dst_width;
        result->height = dst_height;
        result->rgba = (uint8_t *)std::malloc((size_t)dst_width * dst_height * 4);
        if(result->rgba == NULL)
        {
            Upscale_Set_Error("OpenCV: out of memory for a %dx%d result", dst_width, dst_height);
            return UPSCALE_ERR_MEMORY;
        }
        cv::Mat out(dst_height, dst_width, CV_8UC4, result->rgba);
        dst.copyTo(out);

        if(options->encode_png != 0)
        {
            status = Encode_PNG(out, &result->png, &result->png_size);
        }
    }
    catch(const cv::Exception & exception)
    {
        Upscale_Set_Error("OpenCV: resizing %dx%d to %dx%d failed: %s", width, height, dst_width, dst_height, exception.what());
        return UPSCALE_ERR_BACKEND;
    }
    catch(...)
    {
        Upscale_Set_Error("OpenCV: resizing %dx%d to %dx%d failed with an unknown exception", width, height, dst_width, dst_height);
        return UPSCALE_ERR_BACKEND;
    }

    return status;
}


extern "C" int Upscale_OpenCV_Encode_PNG(const uint8_t * rgba, int width, int height, uint8_t ** png, size_t * png_size)
{
    try
    {
        const cv::Mat src(height, width, CV_8UC4, const_cast<uint8_t *>(rgba));
        return Encode_PNG(src, png, png_size);
    }
    catch(const cv::Exception & exception)
    {
        Upscale_Set_Error("OpenCV: encoding a %dx%d PNG failed: %s", width, height, exception.what());
        return UPSCALE_ERR_BACKEND;
    }
    catch(...)
    {
        Upscale_Set_Error("OpenCV: encoding a %dx%d PNG failed with an unknown exception", width, height);
        return UPSCALE_ERR_BACKEND;
    }
}
