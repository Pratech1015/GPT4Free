
// ---- environment shims (Firefox 155 / Linux, matches ChatGPT anonymous web) ----
Object.defineProperty(globalThis, "navigator", { value: {
  userAgent: "Mozilla/5.0 (X11; Linux x86_64; rv:155.0) Gecko/20100101 Firefox/155.0",
  appVersion: "5.0 (X11)",
  platform: "Linux x86_64",
  language: "en-US",
  languages: ["en-US", "en"],
  hardwareConcurrency: 12,
  maxTouchPoints: 0,
  webdriver: true,
  vendor: "",
  buildID: "20260810913b",
  cookieEnabled: true,
  onLine: true,
  plugins: [1, 2, 3],
  mimeTypes: [1, 2],
  javaEnabled() { return false; },
  getBattery: async () => ({ charging: true, chargingTime: 0, dischargingTime: Infinity, level: 1 }),
  storage: {
    estimate: async () => ({ quota: 803722444, usage: 131072 }),
    persist: async () => true,
    persisted: async () => false,
  },
}, configurable: true, writable: true });

Object.defineProperty(globalThis, "screen", { value: {
  availWidth: 1366, availHeight: 768, availLeft: 0, availTop: 0,
  colorDepth: 24, pixelDepth: 24, width: 1366, height: 768,
  orientation: { angle: 0, type: "landscape-primary", onchange: null },
}, configurable: true, writable: true });

const _store = new Map([
  ["oai/apps/lightweight-web/composerDraft/v1", "say hello"],
  ["oai/apps/noAuthUserMessageCount", "1"],
  ["433ab3d185ba2734", "3"],
  ["oai/apps/lightweight-web/composerDraft/owner.v2", '["anonymous",null,null]'],
]);
let _ls = null;
const _lsProto = {};
Object.defineProperties(_lsProto, {
  length: { get: () => _store.size, enumerable: false },
  getItem: { value: (k) => { k = String(k); return Object.prototype.hasOwnProperty.call(_ls, k) ? _ls[k] : null; }, enumerable: false },
  setItem: { value: (k, v) => { k = String(k); v = String(v); _store.set(k, v); _ls[k] = v; }, enumerable: false },
  removeItem: { value: (k) => { k = String(k); _store.delete(k); delete _ls[k]; }, enumerable: false },
  clear: { value: () => { for (const k of Object.keys(_ls)) delete _ls[k]; _store.clear(); }, enumerable: false },
  key: { value: (i) => Object.keys(_ls)[i] ?? null, enumerable: false },
});
_ls = Object.create(_lsProto);
for (const [k, v] of _store) _ls[k] = v;
Object.defineProperty(globalThis, "localStorage", { value: _ls, configurable: true, writable: true });

Object.defineProperty(globalThis, "history", { value: {
  length: 2, state: null, scrollRestoration: "auto",
  back() {}, forward() {}, go() {}, pushState() {}, replaceState() {},
}, configurable: true, writable: true });

// ---- canvas / WebGL fingerprint shim ----
const UNMASKED_VENDOR = 37445, UNMASKED_RENDERER = 37446; // (debug_renderer_info: VENDOR 37445, RENDERER 37446)
const glParams = new Map([
  [7936, "Mozilla"],                         // VENDOR
  [7937, "Radeon HD 3200 Graphics, or similar"], // RENDERER
  [7938, "WebGL 1.0"],
  [35724, "WebGL GLSL ES 1.0"],
  [37445, "AMD"],
  [37446, "Radeon HD 3200 Graphics, or similar"],
  [3379, 16384], [3386, 16384],             // MAX_TEXTURE_SIZE, MAX_RENDERBUFFER_SIZE
  [33901, new Int32Array([1, 16384])],      // ALIASED_POINT_SIZE_RANGE
  [33862, new Float32Array([1, 1])],        // ALIASED_LINE_WIDTH_RANGE? (7233/33862 vary)
  [34024, new Int32Array([16384, 16384])],  // MAX_VIEWPORT_DIMS
  [34921, 16],                              // MAX_VERTEX_ATTRIBS
  [34930, 16],                              // MAX_TEXTURE_IMAGE_UNITS
  [35660, 1024],                            // MAX_VERTEX_UNIFORM_VECTORS
  [35661, 15],                              // MAX_VARYING_VECTORS
  [35662, 16],                              // MAX_FRAGMENT_UNIFORM_VECTORS
  [35658, 32],                              // MAX_VERTEX_TEXTURE_IMAGE_UNITS
  [35657, 32],                              // MAX_COMBINED_TEXTURE_IMAGE_UNITS
  [34076, 16384],                           // MAX_CUBE_MAP_TEXTURE_SIZE
  [34933, 16384],                           // MAX_RENDERBUFFER? (aliased)
  [34047, 16], [34048, 16], [34049, 16],    // SAMPLE_BUFFERS-ish placeholders
  [3410, 8], [3411, 8], [3412, 8], [3413, 8], [3414, 8], [3415, 8], // RED/GREEN/BLUE/ALPHA/DEPTH/STENCIL_BITS
  [32937, 8], [32936, 8],                   // DEPTH/STENCIL_BITS alt
  [2978, 1], [2977, 1], [2979, 1],          // SAMPLE_BUFFERS/SAMPLES
  [2982, new Float32Array([0.1, 100])],     // DEPTH_RANGE? (approx)
  [3106, new Float32Array([0, 0, 0, 0])],   // SCISSOR_BOX-ish
  [2983, new Int32Array([0, 0, 300, 150])], // SCISSOR_BOX
  [2984, new Float32Array([0, 0, 0, 0])],   // COLOR_CLEAR_VALUE
  [2985, false], [2986, false], [2987, false], // blend/depth flags
  [3042, false], [2930, false], [3024, true], // blend, depth test, dither
  [35738, false],                           // polygon offset
  [3088, new Int32Array([0, 0, 300, 150])], // SCISSOR_TEST box
  [3089, new Int32Array([0, 0, 300, 150])],
  [34921, 16],
]);
const stdExtensions = [
  "ANGLE_instanced_arrays", "EXT_blend_minmax", "EXT_color_buffer_half_float",
  "EXT_depth_clamp", "EXT_float_blend", "EXT_frag_depth", "EXT_shader_texture_lod",
  "EXT_sRGB", "EXT_texture_compression_bptc", "EXT_texture_compression_rgtc",
  "EXT_texture_filter_anisotropic", "OES_element_index_uint", "OES_fbo_render_mipmap",
  "OES_standard_derivatives", "OES_texture_float", "OES_texture_float_linear",
  "OES_texture_half_float", "OES_texture_half_float_linear", "OES_vertex_array_object",
  "WEBGL_color_buffer_float", "WEBGL_compressed_texture_astc", "WEBGL_compressed_texture_etc",
  "WEBGL_compressed_texture_s3tc", "WEBGL_compressed_texture_s3tc_srgb",
  "WEBGL_debug_renderer_info", "WEBGL_debug_shaders", "WEBGL_depth_texture",
  "WEBGL_draw_buffers", "WEBGL_lose_context",
];
function makeGL() {
  const gl = {
    canvas: null,
    getParameter(p) {
      if (p === 37445 || p === 37446) return glParams.get(p);
      if (glParams.has(p)) return glParams.get(p);
      if (typeof p !== "number") return null;
      if (p >= 34800 && p <= 34900) return null;
      if (p >= 35600 && p <= 35720) return 0;
      return (p * 2654435761) % 65536;
    },
    getSupportedExtensions: () => stdExtensions.slice(),
    getExtension(name) {
      if (name === "WEBGL_debug_renderer_info")
        return { UNMASKED_VENDOR_WEBGL: 37445, UNMASKED_RENDERER_WEBGL: 37446 };
      if (name === "WEBGL_lose_context") return { loseContext() {}, restoreContext() {} };
      if (name === "OES_texture_float_linear" || name === "OES_standard_derivatives" ||
          name === "OES_vertex_array_object" || name === "EXT_shader_texture_lod" ||
          name === "WEBGL_depth_texture" || name === "ANGLE_instanced_arrays" ||
          name === "EXT_frag_depth" || name === "EXT_draw_buffers" ||
          name === "WEBGL_draw_buffers" || name === "OES_element_index_uint" ||
          name === "EXT_texture_filter_anisotropic" || name === "WEBGL_lose_context")
        return {};
      if (name && name.startsWith("WEBGL_compressed_texture")) return {};
      if (name && name.startsWith("EXT_")) return {};
      if (name && name.startsWith("OES_")) return {};
      if (name && name.startsWith("WEBGL_")) return {};
      return null;
    },
    getShaderPrecisionFormat: () => ({ rangeMin: 127, rangeMax: 127, precision: 23 }),
    getProgramParameter: () => true,
    getShaderParameter: () => true,
    getProgramInfoLog: () => "",
    getShaderInfoLog: () => "",
    getUniformLocation: () => null,
    getAttribLocation: () => 0,
    getError: () => 0,
    isContextLost: () => false,
    getContextAttributes: () => ({ alpha: true, antialias: true, depth: true, desynchronized: false,
      failIfMajorPerformanceCaveat: false, premultipliedAlpha: true, preserveDrawingBuffer: false,
      powerPreference: "default", stencil: false, xrCompatible: false }),
    createShader: () => ({}), shaderSource() {}, compileShader() {},
    createProgram: () => ({}), attachShader() {}, linkProgram() {}, useProgram() {},
    createBuffer: () => ({}), bindBuffer() {}, bufferData() {},
    createTexture: () => ({}), bindTexture() {}, texImage2D() {}, texParameteri() {},
    createFramebuffer: () => ({}), bindFramebuffer() {},
    createRenderbuffer: () => ({}), bindRenderbuffer() {},
    createVertexArray: () => ({}), bindVertexArray() {},
    viewport() {}, clear() {}, clearColor() {}, enable() {}, disable() {},
    drawArrays() {}, drawElements() {}, blendFunc() {}, pixelStorei() {},
    cullFace() {}, frontFace() {}, lineWidth() {}, scissor() {},
    activeTexture() {}, generateMipmap() {},
    getBufferParameter: () => 0, getTexParameter: () => 0,
    checkFramebufferStatus: () => 36053,
    readPixels(x, y, w, h, f, t, p) { if (p && p.fill) p.fill(0); },
    framebufferTexture2D() {}, renderbufferStorage() {}, framebufferRenderbuffer() {},
    texSubImage2D() {}, compressedTexImage2D() {},
    stencilFunc() {}, stencilOp() {}, depthFunc() {}, depthMask() {},
    colorMask() {}, blendFuncSeparate() {}, blendEquation() {},
    uniform1i() {}, uniform1f() {}, uniform2f() {}, uniform4f() {},
    getIndexedParameter: () => 0,
    clientWaitSync: () => 0, fenceSync: () => ({}),
    deleteTexture() {}, deleteBuffer() {}, deleteProgram() {},
  };
  for (const k of ["VERTEX_SHADER", "FRAGMENT_SHADER", "COMPILE_STATUS", "LINK_STATUS",
                   "TEXTURE_2D", "TEXTURE0", "ARRAY_BUFFER", "ELEMENT_ARRAY_BUFFER",
                   "FRAMEBUFFER", "RENDERBUFFER", "COLOR_BUFFER_BIT", "DEPTH_BUFFER_BIT",
                   "STENCIL_BUFFER_BIT", "TRIANGLES", "UNSIGNED_BYTE", "UNSIGNED_SHORT",
                   "UNSIGNED_INT", "FLOAT", "BYTE", "SHORT", "INT", "BOOL",
                   "CULL_FACE", "BLEND", "DEPTH_TEST", "STENCIL_TEST", "SCISSOR_TEST",
                   "DITHER", "POLYGON_OFFSET_FILL", "SAMPLE_ALPHA_TO_COVERAGE",
                   "TEXTURE_MIN_FILTER", "TEXTURE_MAG_FILTER", "LINEAR", "NEAREST",
                   "CLAMP_TO_EDGE", "REPEAT", "RGBA", "RGB", "LUMINANCE", "ALPHA",
                   "VENDOR", "RENDERER", "VERSION", "SHADING_LANGUAGE_VERSION",
                   "MAX_TEXTURE_SIZE", "MAX_RENDERBUFFER_SIZE", "MAX_VIEWPORT_DIMS",
                   "MAX_VERTEX_ATTRIBS", "MAX_TEXTURE_IMAGE_UNITS",
                   "MAX_VERTEX_UNIFORM_VECTORS", "MAX_VARYING_VECTORS",
                   "MAX_FRAGMENT_UNIFORM_VECTORS", "MAX_COMBINED_TEXTURE_IMAGE_UNITS",
                   "MAX_VERTEX_TEXTURE_IMAGE_UNITS", "MAX_CUBE_MAP_TEXTURE_SIZE",
                   "RED_BITS", "GREEN_BITS", "BLUE_BITS", "ALPHA_BITS",
                   "DEPTH_BITS", "STENCIL_BITS", "SAMPLE_BUFFERS", "SAMPLES",
                   "SUBPIXEL_BITS", "STEREO", "UNMASKED_VENDOR_WEBGL",
                   "UNMASKED_RENDERER_WEBGL", "CONTEXT_FLAGS",
                   "CONTEXT_LOST_WEBGL", "IMPLEMENTATION_COLOR_READ_FORMAT",
                   "IMPLEMENTATION_COLOR_READ_TYPE", "LOW_FLOAT", "MEDIUM_FLOAT",
                   "HIGH_FLOAT", "LOW_INT", "MEDIUM_INT", "HIGH_INT"])
    gl[k] = 0;
  // enum values commonly read as properties
  Object.assign(gl, {
    VERTEX_SHADER: 35633, FRAGMENT_SHADER: 35632, COMPILE_STATUS: 35713,
    LINK_STATUS: 35714, TEXTURE_2D: 3553, TEXTURE0: 33984, ARRAY_BUFFER: 34962,
    ELEMENT_ARRAY_BUFFER: 34963, FRAMEBUFFER: 36160, RENDERBUFFER: 36161,
    COLOR_BUFFER_BIT: 16384, DEPTH_BUFFER_BIT: 256, STENCIL_BUFFER_BIT: 1024,
    TRIANGLES: 4, UNSIGNED_BYTE: 5121, UNSIGNED_SHORT: 5123, UNSIGNED_INT: 5125,
    FLOAT: 5126, BYTE: 5120, SHORT: 5122, INT: 5124, BOOL: 35670,
    CULL_FACE: 2884, BLEND: 3042, DEPTH_TEST: 2929, STENCIL_TEST: 2960,
    SCISSOR_TEST: 3089, DITHER: 3024, POLYGON_OFFSET_FILL: 32823,
    TEXTURE_MIN_FILTER: 10241, TEXTURE_MAG_FILTER: 10240, LINEAR: 9729,
    NEAREST: 9728, CLAMP_TO_EDGE: 33071, REPEAT: 10497, RGBA: 6408, RGB: 6407,
    VENDOR: 7936, RENDERER: 7937, VERSION: 7938, SHADING_LANGUAGE_VERSION: 35724,
    MAX_TEXTURE_SIZE: 3379, MAX_RENDERBUFFER_SIZE: 34024, MAX_VIEWPORT_DIMS: 3386,
    MAX_VERTEX_ATTRIBS: 34921, MAX_TEXTURE_IMAGE_UNITS: 34930,
    MAX_VERTEX_UNIFORM_VECTORS: 35658, MAX_VARYING_VECTORS: 35661,
    MAX_FRAGMENT_UNIFORM_VECTORS: 35660, MAX_COMBINED_TEXTURE_IMAGE_UNITS: 35660,
    MAX_VERTEX_TEXTURE_IMAGE_UNITS: 35657, MAX_CUBE_MAP_TEXTURE_SIZE: 34076,
    RED_BITS: 3410, GREEN_BITS: 3411, BLUE_BITS: 3412, ALPHA_BITS: 3413,
    DEPTH_BITS: 3414, STENCIL_BITS: 3415, SAMPLE_BUFFERS: 32940, SAMPLES: 32941,
    SUBPIXEL_BITS: 3413, STEREO: 31230, UNMASKED_VENDOR_WEBGL: 37445,
    UNMASKED_RENDERER_WEBGL: 37446, LOW_FLOAT: 36336, MEDIUM_FLOAT: 36337,
    HIGH_FLOAT: 36338, LOW_INT: 36339, MEDIUM_INT: 36340, HIGH_INT: 36341,
    CONTEXT_LOST_WEBGL: 37442,
  });
  return gl;
}
function make2D() {
  return {
    canvas: null,
    fillStyle: "#000", strokeStyle: "#000", lineWidth: 1, font: "10px sans-serif",
    textAlign: "start", textBaseline: "alphabetic", globalAlpha: 1,
    globalCompositeOperation: "source-over", imageSmoothingEnabled: true,
    lineCap: "butt", lineJoin: "miter", miterLimit: 10, shadowBlur: 0,
    shadowColor: "rgba(0, 0, 0, 0)", filter: "none", direction: "ltr",
    measureText: (t) => ({ width: [...String(t)].reduce((a, c) => a + (c.charCodeAt(0) % 3) * 3.5 + 6, 0) }),
    fillText() {}, strokeText() {}, fillRect() {}, strokeRect() {}, clearRect() {},
    beginPath() {}, closePath() {}, moveTo() {}, lineTo() {}, arc() {}, arcTo() {},
    quadraticCurveTo() {}, bezierCurveTo() {}, rect() {}, fill() {}, stroke() {},
    clip() {}, save() {}, restore() {}, translate() {}, rotate() {}, scale() {},
    transform() {}, setTransform() {}, resetTransform() {}, drawImage() {},
    createLinearGradient: () => ({ addColorStop() {} }),
    createRadialGradient: () => ({ addColorStop() {} }),
    createPattern: () => ({}),
    getImageData(x, y, w, h) {
      const n = Math.max(0, Math.min(1 << 24, w * h * 4));
      const data = new Uint8ClampedArray(n);
      for (let i = 0; i < n; i++) data[i] = (i * 37 + 17) & 255;
      return { data, width: w, height: h, colorSpace: "srgb" };
    },
    putImageData() {}, createImageData(w, h) {
      const n = Math.max(0, Math.min(1 << 24, (w.width || w) * (h || w.height || 1) * 4));
      return { data: new Uint8ClampedArray(n), width: w.width || w, height: h || w.height || 1 };
    },
    isPointInPath: () => false, getTransform: () => ({ a: 1, b: 0, c: 0, d: 1, e: 0, f: 0 }),
    getLineDash: () => [], setLineDash() {},
  };
}
function makeCanvas() {
  const c = {
    width: 300, height: 150, style: {}, offsetWidth: 300, offsetHeight: 150,
    clientWidth: 300, clientHeight: 150, tagName: "CANVAS", nodeName: "CANVAS",
    getContext(type) {
      if (type === "2d") { const g = make2D(); g.canvas = c; return g; }
      if (type === "webgl" || type === "experimental-webgl" || type === "webgl2") {
        const g = makeGL(); g.canvas = c; return g;
      }
      return null;
    },
    toDataURL: () => "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
    toBlob(cb) { cb(null); },
    getBoundingClientRect: () => ({ top: 0, left: 0, width: 300, height: 150, right: 300, bottom: 150, x: 0, y: 0 }),
    addEventListener() {}, removeEventListener() {},
    setAttribute() {}, getAttribute: () => null, removeAttribute() {},
    appendChild(ch) { return ch; }, removeChild(ch) { return ch; },
    cloneNode: () => makeCanvas(),
    ownerDocument: null,
  };
  return c;
}

// location of the challenge page (loaded at https://chatgpt.com/)
Object.defineProperty(globalThis, "location", { value: {
  origin: "https://chatgpt.com", href: "https://chatgpt.com/", protocol: "https:",
  host: "chatgpt.com", hostname: "chatgpt.com", port: "", pathname: "/", search: "", hash: "",
  toString() { return this.href; },
}, configurable: true, writable: true });

Object.defineProperty(globalThis, "document", { value: {
  location: globalThis.location,
  URL: "https://chatgpt.com/",
  documentURI: "https://chatgpt.com/",
  referrer: "",
  visibilityState: "visible", hidden: false, frozen: false,
  hasFocus: () => true,
  cookie: "",
  scripts: [{ src: "https://chatgpt.com/sentinel/20260810913b/sdk.js", type: "", async: true, crossOrigin: null }],
  body: { clientWidth: 1920, clientHeight: 1040, scrollWidth: 1920, scrollHeight: 3000, offsetWidth: 1920, offsetHeight: 1040, scrollTop: 0, scrollLeft: 0, innerHTML: "", style: {}, getBoundingClientRect: () => ({ top:0, left:0, width:1920, height:1040, right:1920, bottom:1040, x:0, y:0 }), appendChild(ch){return ch;}, removeChild(ch){return ch;}, contains: () => false, addEventListener() {}, removeEventListener() {} },
  documentElement: { clientWidth: 1920, clientHeight: 1040, scrollWidth: 1920, scrollHeight: 3000, style: {}, getBoundingClientRect: () => ({ top:0, left:0, width:1920, height:1040, right:1920, bottom:1040, x:0, y:0 }), offsetWidth: 1920, offsetHeight: 1040 },
  createElement: (tag) => {
    if (String(tag).toLowerCase() === "canvas") return makeCanvas();
    return { style: {}, tagName: String(tag).toUpperCase(), setAttribute() {}, getAttribute: () => null,
             appendChild(ch) { return ch; }, removeChild(ch) { return ch; }, addEventListener() {},
             removeEventListener() {}, cloneNode: () => ({ style: {} }),
             clientWidth: 0, clientHeight: 0, offsetWidth: 0, offsetHeight: 0, offsetTop: 0, offsetLeft: 0,
             scrollWidth: 0, scrollHeight: 0, scrollTop: 0, scrollLeft: 0, innerHTML: "", textContent: "",
             getBoundingClientRect: () => ({ x:0, y:720, width:31.399993896484375, height:30, top:720, right:31.399993896484375, bottom:750, left:0 }),
             contains: () => false, focus() {}, blur() {} };
  },
  createTextNode: (t) => ({ nodeValue: t }),
  getElementById: () => null,
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener() {}, removeEventListener() {},
}, configurable: true, writable: true });

// window === globalThis, like in a browser
globalThis.window = globalThis;
if (!globalThis.innerWidth) globalThis.innerWidth = 1920;
if (!globalThis.innerHeight) globalThis.innerHeight = 1040;
if (!globalThis.devicePixelRatio) globalThis.devicePixelRatio = 1;

// ---- VM constants ----
var tQt, nQt, rQt, iQt, aQt, oQt, sQt, cQt, lQt, uQt, A2, dQt, fQt, pQt, mQt, hQt, gQt,
    j2, _Qt, vQt, yQt, bQt, xQt, SQt, CQt, wQt, TQt, EQt, DQt, OQt, kQt, AQt, jQt, MQt, $, M2, NQt;
tQt=0,nQt=1,rQt=2,iQt=3,aQt=4,oQt=5,sQt=6,cQt=24,lQt=7,uQt=8,A2=9,dQt=10,fQt=11,pQt=12,mQt=13,hQt=14,gQt=15,j2=16,_Qt=17,vQt=18,yQt=19,bQt=23,xQt=20,SQt=21,CQt=22,wQt=25,TQt=26,EQt=27,DQt=28,OQt=29,kQt=30,AQt=33,jQt=34,MQt=35,$=new Map,M2=0,NQt=Promise.resolve();
// stub for the WeakMap lookup (we pass p directly)
function nZt(e) { return e && e.__p || ""; }
function tZt() {}
function XZt(e){let t=NQt.then(e,e);return NQt=t.then(()=>void 0,()=>void 0),t}async function k2(){for(;$.get(A2).length>0;){let[e,...t]=$.get(A2).shift(),n=$.get(e)(...t);n&&typeof n.then==`function`&&await n,M2++}}function ZZt(e){return XZt(()=>new Promise((t,n)=>{let r=!1;setTimeout(()=>{r=!0,t(``+M2)},500),$.set(iQt,e=>{r||(r=!0,t(btoa(``+e)))}),$.set(aQt,e=>{r||(r=!0,n(btoa(``+e)))}),$.set(kQt,(e,t,n,i)=>{let a=Array.isArray(i),o=a?n:[],s=(a?i:n)||[];$.set(e,(...e)=>{if(r)return;let n=[...$.get(A2)];if(a)for(let t=0;t<o.length;t++){let n=o[t],r=e[t];$.set(n,r)}return $.set(A2,[...s]),k2().then(()=>$.get(t)).catch(e=>``+e).finally(()=>{$.set(A2,n)})})});try{$.set(A2,JSON.parse($Zt(atob(e),``+$.get(j2)))),k2().catch(e=>{t(btoa(M2+`: `+e))})}catch(e){t(btoa(M2+`: `+e))}}))}function QZt(e,t){return XZt(()=>new Promise((n,r)=>{let i=nZt(e??{})??``;eQt(),_inst(),M2=0,$.set(j2,i);let a=!1;setTimeout(()=>{a=!0,n(``+M2)},500),$.set(iQt,e=>{a||(a=!0,n(btoa(``+e)))}),$.set(aQt,e=>{a||(a=!0,r(btoa(``+e)))}),$.set(kQt,(e,t,n,r)=>{let i=Array.isArray(r),o=i?n:[],s=(i?r:n)||[];$.set(e,(...e)=>{if(a)return;let n=[...$.get(A2)];if(i)for(let t=0;t<o.length;t++){let n=o[t],r=e[t];$.set(n,r)}return $.set(A2,[...s]),k2().then(()=>$.get(t)).catch(e=>``+e).finally(()=>{$.set(A2,n)})})});try{$.set(A2,JSON.parse($Zt(atob(t),``+$.get(j2)))),k2().catch(e=>{n(btoa(M2+`: `+e))})}catch(e){n(btoa(M2+`: `+e))}}))}function $Zt(e,t){let n=``;for(let r=0;r<e.length;r++)n+=String.fromCharCode(e.charCodeAt(r)^t.charCodeAt(r%t.length));return n}function eQt(){$.clear(),$.set(tQt,ZZt),$.set(nQt,(e,t)=>$.set(e,$Zt(``+$.get(e),``+$.get(t)))),$.set(rQt,(e,t)=>$.set(e,t)),$.set(oQt,(e,t)=>{let n=$.get(e);Array.isArray(n)?n.push($.get(t)):$.set(e,n+$.get(t))}),$.set(EQt,(e,t)=>{let n=$.get(e);Array.isArray(n)?n.splice(n.indexOf($.get(t)),1):$.set(e,n-$.get(t))}),$.set(OQt,(e,t,n)=>$.set(e,$.get(t)<$.get(n))),$.set(AQt,(e,t,n)=>{let r=Number($.get(t)),i=Number($.get(n));$.set(e,r*i)}),$.set(MQt,(e,t,n)=>{let r=Number($.get(t)),i=Number($.get(n));$.set(e,i===0?0:r/i)}),$.set(sQt,(e,t,n)=>$.set(e,$.get(t)[$.get(n)])),$.set(lQt,(e,...t)=>$.get(e)(...t.map(e=>$.get(e)))),$.set(_Qt,(e,t,...n)=>{try{let r=$.get(t)(...n.map(e=>$.get(e)));if(r&&typeof r.then==`function`)return r.then(t=>{$.set(e,t)}).catch(t=>{$.set(e,``+t)});$.set(e,r)}catch(t){$.set(e,``+t)}}),$.set(mQt,(e,t,...n)=>{try{$.get(t)(...n)}catch(t){$.set(e,``+t)}}),$.set(uQt,(e,t)=>$.set(e,$.get(t))),$.set(dQt,window),$.set(fQt,(e,t)=>$.set(e,(Array.from(document.scripts||[]).map(e=>e?.src?.match($.get(t))).filter(e=>e?.length)[0]??[])[0]??null)),$.set(pQt,e=>$.set(e,$)),$.set(hQt,(e,t)=>$.set(e,JSON.parse(``+$.get(t)))),$.set(gQt,(e,t)=>$.set(e,JSON.stringify($.get(t)))),$.set(vQt,e=>$.set(e,atob(``+$.get(e)))),$.set(yQt,e=>$.set(e,btoa(``+$.get(e)))),$.set(xQt,(e,t,n,...r)=>$.get(e)===$.get(t)?$.get(n)(...r):null),$.set(SQt,(e,t,n,r,...i)=>Math.abs($.get(e)-$.get(t))>$.get(n)?$.get(r)(...i):null),$.set(bQt,(e,t,...n)=>$.get(e)===void 0?null:$.get(t)(...n)),$.set(cQt,(e,t,n)=>$.set(e,$.get(t)[$.get(n)].bind($.get(t)))),$.set(jQt,(e,t)=>{try{let n=$.get(t);return Promise.resolve(n).then(t=>{$.set(e,t)})}catch{return}}),$.set(CQt,(e,t)=>{let n=[...$.get(A2)];return $.set(A2,[...t]),k2().catch(t=>{$.set(e,``+t)}).finally(()=>{$.set(A2,n)})}),$.set(DQt,()=>{}),$.set(TQt,()=>{}),$.set(wQt,()=>{})}
function _inst() {
  // Firefox-style error message translation (V8 text differs from SpiderMonkey)
  const APOSTROPHE = String.fromCharCode(39);
  const DQ = String.fromCharCode(34);
  const ffFix = (s) => {
    if (typeof s !== "string") return s;
    let m = s.match(/^(TypeError: )?Cannot read properties of undefined \(reading '(.+?)'\)$/);
    if (m) return (m[1] || "") + "can" + APOSTROPHE + "t access property " + DQ + m[2] + DQ + ", o.get(...) is undefined";
    m = s.match(/^(TypeError: )?Cannot read properties of null \(reading '(.+?)'\)$/);
    if (m) return (m[1] || "") + "can" + APOSTROPHE + "t access property " + DQ + m[2] + DQ + ", o.get(...) is null";
    return s;
  };
  for (const id of [13, 17, 34]) { // mQt tryCall, _Qt tryCallStore, jQt then
    const o = $.get(id);
    if (typeof o !== "function") continue;
    $.set(id, (...a) => {
      const dst = a[0];
      const r = o(...a);
      try { const cur = $.get(dst); if (typeof cur === "string" && cur.includes("Cannot read properties")) $.set(dst, ffFix(cur)); } catch (e) {}
      return r;
    });
  }
}
export async function solveTurnstile(pBlob, dx, timeoutMs = 3000) {
  M2 = 0;
  const t0 = Date.now();
  const token = await Promise.race([
    QZt({ __p: pBlob }, dx),
    new Promise((res) => setTimeout(() => res("TIMEOUT_" + M2), timeoutMs)),
  ]);
  return { token, steps: M2, ms: Date.now() - t0 };
}
