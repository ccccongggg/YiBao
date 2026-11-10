/*
 * ptz_daemon.c - Insta360 Link 2 常驻云台守护进程
 *
 * 解决 v1 每次命令都重启进程（1-2s 延迟）的问题：
 * 本进程启动时建立 DirectShow IAMCameraControl 连接并常驻，
 * 通过 stdin/stdout 行协议响应命令，单命令延迟 <10ms。
 *
 * 协议（每行一条命令，每命令回一行结果，fflush 立即刷新）：
 *   PING                 -> OK 0
 *   RANGE                -> OK pmin pmax tmin tmax zmin zmax zstep zdef
 *   GET                  -> OK pan tilt zoom
 *   SET <pan> <tilt>     -> OK pan tilt zoom
 *   ZOOM <z>             -> OK pan tilt zoom
 *   QUIT                 -> (退出)
 *   其他                 -> ERR bad_cmd / ERR <原因>
 *
 * Build (MinGW-w64):
 *   gcc ptz_daemon.c -lole32 -loleaut32 -lstrmiids -o ptz_daemon.exe
 */
#define COBJMACROS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <initguid.h>
#include <dshow.h>
#include <strmif.h>
#include <stdio.h>
#include <string.h>

#define DEV_MATCH L"Insta360 Link 2"

static IBaseFilter *find_camera(void) {
  ICreateDevEnum *dev_enum = NULL;
  IEnumMoniker *enum_mon = NULL;
  IBaseFilter *result = NULL;

  HRESULT hr = CoCreateInstance(&CLSID_SystemDeviceEnum, NULL, CLSCTX_INPROC_SERVER,
                                &IID_ICreateDevEnum, (void **)&dev_enum);
  if (FAILED(hr)) return NULL;

  hr = ICreateDevEnum_CreateClassEnumerator(dev_enum, &CLSID_VideoInputDeviceCategory, &enum_mon, 0);
  if (hr != S_OK || !enum_mon) { ICreateDevEnum_Release(dev_enum); return NULL; }

  IMoniker *mon = NULL;
  while (!result && IEnumMoniker_Next(enum_mon, 1, &mon, NULL) == S_OK) {
    IPropertyBag *bag = NULL;
    if (SUCCEEDED(IMoniker_BindToStorage(mon, NULL, NULL, &IID_IPropertyBag, (void **)&bag))) {
      VARIANT name;
      VariantInit(&name);
      if (SUCCEEDED(IPropertyBag_Read(bag, L"FriendlyName", &name, NULL))) {
        if (name.vt == VT_BSTR && wcsstr(name.bstrVal, DEV_MATCH)) {
          IMoniker_BindToObject(mon, NULL, NULL, &IID_IBaseFilter, (void **)&result);
        }
      }
      VariantClear(&name);
      IPropertyBag_Release(bag);
    }
    IMoniker_Release(mon);
    mon = NULL;
  }
  IEnumMoniker_Release(enum_mon);
  ICreateDevEnum_Release(dev_enum);
  return result;
}

static long clamp_l(long v, long lo, long hi) {
  if (hi > lo) { if (v < lo) v = lo; if (v > hi) v = hi; }
  return v;
}

int main(void) {
  if (FAILED(CoInitializeEx(NULL, COINIT_MULTITHREADED))) {
    printf("ERR com_init\n"); fflush(stdout); return 1;
  }

  IBaseFilter *cam = find_camera();
  if (!cam) {
    printf("ERR no_device\n"); fflush(stdout);
    CoUninitialize(); return 1;
  }

  IAMCameraControl *cc = NULL;
  if (FAILED(IBaseFilter_QueryInterface(cam, &IID_IAMCameraControl, (void **)&cc))) {
    printf("ERR no_camera_control\n"); fflush(stdout);
    IBaseFilter_Release(cam); CoUninitialize(); return 1;
  }

  long pmin = 0, pmax = 0, ps = 0, pd = 0, pf = 0;
  long tmin = 0, tmax = 0, ts = 0, td = 0, tf = 0;
  long zmin = 0, zmax = 0, zs = 0, zd = 0, zf = 0;
  BOOL zoom_ok = SUCCEEDED(IAMCameraControl_GetRange(cc, CameraControl_Zoom, &zmin, &zmax, &zs, &zd, &zf));
  if (!zoom_ok) { zmin = zmax = zs = zd = 0; }
  IAMCameraControl_GetRange(cc, CameraControl_Pan, &pmin, &pmax, &ps, &pd, &pf);
  IAMCameraControl_GetRange(cc, CameraControl_Tilt, &tmin, &tmax, &ts, &td, &tf);

  char line[256];
  while (fgets(line, sizeof(line), stdin)) {
    char cmd[32] = "";
    if (sscanf(line, "%31s", cmd) != 1) { printf("ERR empty\n"); fflush(stdout); continue; }

    if (_stricmp(cmd, "PING") == 0) {
      printf("OK 0\n");
    } else if (_stricmp(cmd, "RANGE") == 0) {
      printf("OK %ld %ld %ld %ld %ld %ld %ld %ld\n", pmin, pmax, tmin, tmax, zmin, zmax, zs, zd);
    } else if (_stricmp(cmd, "GET") == 0) {
      long pan = 0, tilt = 0, zoom = 0, f1 = 0, f2 = 0, f3 = 0;
      HRESULT h1 = IAMCameraControl_Get(cc, CameraControl_Pan, &pan, &f1);
      HRESULT h2 = IAMCameraControl_Get(cc, CameraControl_Tilt, &tilt, &f2);
      HRESULT h3 = zoom_ok ? IAMCameraControl_Get(cc, CameraControl_Zoom, &zoom, &f3) : E_NOTIMPL;
      if (FAILED(h1) || FAILED(h2)) printf("ERR read\n");
      else printf("OK %ld %ld %ld\n", pan, tilt, SUCCEEDED(h3) ? zoom : 0);
    } else if (_stricmp(cmd, "SET") == 0) {
      long p = 0, t = 0;
      if (sscanf(line, "%*s %ld %ld", &p, &t) != 2) { printf("ERR args\n"); }
      else {
        p = clamp_l(p, pmin, pmax);
        t = clamp_l(t, tmin, tmax);
        HRESULT h1 = IAMCameraControl_Set(cc, CameraControl_Pan, p, CameraControl_Flags_Manual);
        HRESULT h2 = IAMCameraControl_Set(cc, CameraControl_Tilt, t, CameraControl_Flags_Manual);
        if (FAILED(h1) || FAILED(h2)) printf("ERR set\n");
        else {
          long zoom = 0, f3 = 0;
          if (zoom_ok) IAMCameraControl_Get(cc, CameraControl_Zoom, &zoom, &f3);
          printf("OK %ld %ld %ld\n", p, t, zoom);
        }
      }
    } else if (_stricmp(cmd, "ZOOM") == 0) {
      long z = 0;
      if (!zoom_ok) { printf("ERR zoom_unsupported\n"); }
      else if (sscanf(line, "%*s %ld", &z) != 1) { printf("ERR args\n"); }
      else {
        z = clamp_l(z, zmin, zmax);
        if (FAILED(IAMCameraControl_Set(cc, CameraControl_Zoom, z, CameraControl_Flags_Manual)))
          printf("ERR zoom_set\n");
        else printf("OK %ld %ld %ld\n", 0, 0, z);
      }
    } else if (_stricmp(cmd, "QUIT") == 0) {
      fflush(stdout);
      break;
    } else {
      printf("ERR bad_cmd\n");
    }
    fflush(stdout);
  }

  IAMCameraControl_Release(cc);
  IBaseFilter_Release(cam);
  CoUninitialize();
  return 0;
}
