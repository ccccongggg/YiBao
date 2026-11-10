/*
 * ptz_dshow.c - Insta360 Link 2 pan/tilt control via DirectShow IAMCameraControl
 *
 * Windows-native path: usbvideo.sys maps KSPROPERTY_CAMERACONTROL_PAN/TILT
 * to UVC CT_PANTILT_ABSOLUTE_CONTROL (selector 0x0D) on the Camera Terminal,
 * using the DEFAULT system UVC driver. No libusb / no driver swap needed.
 *
 * Usage:
 *   ptz_dshow.exe              print ranges + current pan/tilt (human readable)
 *   ptz_dshow.exe j            print {"pan":..,"tilt":..} only (for scripts)
 *   ptz_dshow.exe <pan> <tilt> set absolute pan/tilt, then print JSON result
 *
 * Build (MinGW-w64):
 *   gcc ptz_dshow.c -lole32 -loleaut32 -lstrmiids -o ptz_dshow.exe
 */
#define COBJMACROS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <initguid.h>
#include <dshow.h>
#include <strmif.h>
#include <stdio.h>
#include <stdlib.h>

#define DEV_MATCH L"Insta360 Link 2"

static int g_quiet = 0;

static IBaseFilter *find_camera(void) {
  ICreateDevEnum *dev_enum = NULL;
  IEnumMoniker *enum_mon = NULL;
  IBaseFilter *result = NULL;

  HRESULT hr = CoCreateInstance(&CLSID_SystemDeviceEnum, NULL, CLSCTX_INPROC_SERVER,
                                &IID_ICreateDevEnum, (void **)&dev_enum);
  if (FAILED(hr)) { fwprintf(stderr, L"CoCreateInstance(SystemDeviceEnum) failed 0x%08lx\n", hr); return NULL; }

  hr = ICreateDevEnum_CreateClassEnumerator(dev_enum, &CLSID_VideoInputDeviceCategory, &enum_mon, 0);
  if (hr != S_OK || !enum_mon) {
    fwprintf(stderr, L"no video input devices\n");
    ICreateDevEnum_Release(dev_enum);
    return NULL;
  }

  IMoniker *mon = NULL;
  while (!result && IEnumMoniker_Next(enum_mon, 1, &mon, NULL) == S_OK) {
    IPropertyBag *bag = NULL;
    if (SUCCEEDED(IMoniker_BindToStorage(mon, NULL, NULL, &IID_IPropertyBag, (void **)&bag))) {
      VARIANT name;
      VariantInit(&name);
      if (SUCCEEDED(IPropertyBag_Read(bag, L"FriendlyName", &name, NULL))) {
        if (name.vt == VT_BSTR && wcsstr(name.bstrVal, DEV_MATCH)) {
          if (!g_quiet) wprintf(L"[device] %ls\n", name.bstrVal);
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
  if (!result) fwprintf(stderr, L"device '%ls' not found in DirectShow\n", DEV_MATCH);
  return result;
}

static const char *flagstr(long f) {
  switch (f) {
    case CameraControl_Flags_Manual: return "manual";
    case CameraControl_Flags_Auto:   return "auto";
    default: return "unknown";
  }
}

int main(int argc, char **argv) {
  HRESULT hr = CoInitializeEx(NULL, COINIT_MULTITHREADED);
  if (FAILED(hr)) { fwprintf(stderr, L"CoInitializeEx failed\n"); return 1; }

  BOOL json_mode = (argc >= 2 && strcmp(argv[1], "j") == 0);
  BOOL set_mode = (argc >= 3);
  g_quiet = json_mode || set_mode;

  IBaseFilter *cam = find_camera();
  if (!cam) { CoUninitialize(); return 1; }

  IAMCameraControl *cc = NULL;
  hr = IBaseFilter_QueryInterface(cam, &IID_IAMCameraControl, (void **)&cc);
  if (FAILED(hr)) {
    fwprintf(stderr, L"IAMCameraControl not supported (0x%08lx)\n", hr);
    IBaseFilter_Release(cam); CoUninitialize(); return 1;
  }

  long pmin = 0, pmax = 0, pstep = 0, pdef = 0, pflags = 0;
  long tmin = 0, tmax = 0, tstep = 0, tdef = 0, tflags = 0;

  if (SUCCEEDED(IAMCameraControl_GetRange(cc, CameraControl_Pan, &pmin, &pmax, &pstep, &pdef, &pflags)))
    if (!json_mode && !set_mode) wprintf(L"[pan ] range %ld..%ld step %ld default %ld flags %ld(%hs)\n", pmin, pmax, pstep, pdef, pflags, flagstr(pflags));
  if (SUCCEEDED(IAMCameraControl_GetRange(cc, CameraControl_Tilt, &tmin, &tmax, &tstep, &tdef, &tflags)))
    if (!json_mode && !set_mode) wprintf(L"[tilt] range %ld..%ld step %ld default %ld flags %ld(%hs)\n", tmin, tmax, tstep, tdef, tflags, flagstr(tflags));

  long pan = 0, tilt = 0, pf = 0, tf = 0;
  BOOL have_cur = SUCCEEDED(IAMCameraControl_Get(cc, CameraControl_Pan, &pan, &pf)) &&
                  SUCCEEDED(IAMCameraControl_Get(cc, CameraControl_Tilt, &tilt, &tf));
  if (json_mode && !have_cur) {
    fwprintf(stderr, L"read failed\n");
    IAMCameraControl_Release(cc); IBaseFilter_Release(cam); CoUninitialize();
    return 1;
  }
  if (!json_mode && !set_mode && have_cur)
    wprintf(L"[cur ] pan=%ld (%hs) tilt=%ld (%hs)\n", pan, flagstr(pf), tilt, flagstr(tf));

  int rc = 0;
  if (argc >= 3) {
    long np = strtol(argv[1], NULL, 0);
    long nt = strtol(argv[2], NULL, 0);
    /* clamp to reported hardware range when available */
    if (pmax > pmin) { if (np < pmin) np = pmin; if (np > pmax) np = pmax; }
    if (tmax > tmin) { if (nt < tmin) nt = tmin; if (nt > tmax) nt = tmax; }
    hr = IAMCameraControl_Set(cc, CameraControl_Pan, np, CameraControl_Flags_Manual);
    if (FAILED(hr)) { fwprintf(stderr, L"Set pan failed 0x%08lx\n", hr); rc = 1; }
    hr = IAMCameraControl_Set(cc, CameraControl_Tilt, nt, CameraControl_Flags_Manual);
    if (FAILED(hr)) { fwprintf(stderr, L"Set tilt failed 0x%08lx\n", hr); rc = 1; }
    if (!rc) {
      Sleep(250);
      if (SUCCEEDED(IAMCameraControl_Get(cc, CameraControl_Pan, &pan, &pf)) &&
          SUCCEEDED(IAMCameraControl_Get(cc, CameraControl_Tilt, &tilt, &tf))) {
        if (json_mode || set_mode) printf("{\"pan\":%ld,\"tilt\":%ld}\n", pan, tilt);
        else wprintf(L"[new ] pan=%ld tilt=%ld\n", pan, tilt);
      }
    }
  } else if (json_mode) {
    printf("{\"pan\":%ld,\"tilt\":%ld}\n", pan, tilt);
  }

  IAMCameraControl_Release(cc);
  IBaseFilter_Release(cam);
  CoUninitialize();
  return rc;
}
