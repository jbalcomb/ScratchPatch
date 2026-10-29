/* Test harness for tests/test_engine_parity.py: ReMoM's FLIC decode functions are pasted in where marked. */
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>
#include <assert.h>
typedef unsigned char * byte_ptr; typedef unsigned char byte;
#define SCREEN_WIDTH 320
#define SCREEN_HEIGHT 200
#define SCREEN_XMIN 0
#define SCREEN_YMIN 0
#define SCREEN_XMAX 319
#define SCREEN_YMAX 199
#define SZ_FLIC_HDR 16
#define FLIC_GET_WIDTH(p) GET_2B_OFS(p,0)
#define FLIC_GET_HEIGHT(p) GET_2B_OFS(p,2)
#define GET_2B_OFS(p,o) (*(int16_t*)((p)+(o)))
static unsigned char screen[320*200 + 4096];
static unsigned char *current_video_page = screen;
static unsigned char remap_color_palettes[24*256];
/* ENGINE FUNCTIONS */

/* usage: harness mode(0 draw,1 remap,2 bitmap) w h fill < framedata ; prints w*h bytes row-major */
int main(int argc, char **argv) {
    int mode = atoi(argv[1]), w = atoi(argv[2]), h = atoi(argv[3]), fill = atoi(argv[4]);
    static unsigned char data[1<<20]; fread(data, 1, sizeof data, stdin);
    for (int i = 0; i < 24*256; i++) remap_color_palettes[i] = (unsigned char)(i * 7 + 3);
    memset(screen, fill, sizeof screen);
    if (mode == 0) FLIC_Draw_Frame(0, 0, w, data, h);
    else if (mode == 1) FLIC_Remap_Draw_Frame(0, 0, w, data, h);
    else {
        static unsigned char bm[16 + 320*200];
        memset(bm, fill, sizeof bm); *(int16_t*)bm = w; *(int16_t*)(bm+2) = h;
        Add_Picture_To_Bitmap(data, bm);
        for (int y = 0; y < h; y++) for (int x = 0; x < w; x++) putchar(bm[16 + x*h + y]);   /* bitmaps are column-major */
        return 0;
    }
    for (int y = 0; y < h; y++) for (int x = 0; x < w; x++) putchar(screen[y*320 + x]);
    return 0;
}
