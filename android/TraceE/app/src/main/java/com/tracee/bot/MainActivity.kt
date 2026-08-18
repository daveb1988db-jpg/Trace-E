package com.tracee.bot

import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.ActivityInfo
import android.content.res.Configuration
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.Bundle
import android.view.InputDevice
import android.view.KeyEvent
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.webkit.JavascriptInterface
import android.webkit.WebChromeClient
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors

/**
 * Trace-E WEB-Quarters shell: loads local assets/www control UI.
 * MJPEG via [MjpegProxyServer]; drive hits ESP :8765 directly from JS.
 * [TraceBridge.probeEsp] uses HttpURLConnection so file:// WebView is not blocked by CORS.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private var proxy: MjpegProxyServer? = null
    private val io = Executors.newCachedThreadPool()

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WindowCompat.setDecorFitsSystemWindows(window, false)

        webView = WebView(this)
        setContentView(webView)

        proxy = MjpegProxyServer(defaultEspStream = DEFAULT_ESP_STREAM).also { it.start() }

        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false
            mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
            cacheMode = WebSettings.LOAD_NO_CACHE
            allowFileAccess = true
            @Suppress("DEPRECATION")
            allowUniversalAccessFromFileURLs = true
            @Suppress("DEPRECATION")
            allowFileAccessFromFileURLs = true
            blockNetworkLoads = false
            blockNetworkImage = false
        }
        webView.webChromeClient = WebChromeClient()
        webView.webViewClient = object : WebViewClient() {
            @Deprecated("Deprecated in Java")
            override fun shouldOverrideUrlLoading(view: WebView?, url: String?): Boolean = false
        }
        webView.addJavascriptInterface(TraceBridge(), "TraceEAndroid")
        webView.setLayerType(View.LAYER_TYPE_HARDWARE, null)

        applyOrientationUi(resources.configuration.orientation)
        webView.loadUrl("file:///android_asset/www/index.html")
    }

    override fun onConfigurationChanged(newConfig: Configuration) {
        super.onConfigurationChanged(newConfig)
        applyOrientationUi(newConfig.orientation)
    }

    private fun applyOrientationUi(orientation: Int) {
        val landscape = orientation == Configuration.ORIENTATION_LANDSCAPE
        val controller = WindowInsetsControllerCompat(window, window.decorView)
        if (landscape) {
            window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            controller.hide(WindowInsetsCompat.Type.systemBars())
            controller.systemBarsBehavior =
                WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
        } else {
            window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            controller.show(WindowInsetsCompat.Type.systemBars())
        }
    }

    /* ---- Bluetooth gamepad bridge ----
       A pad paired to the tablet arrives as Activity key/motion events. The
       WebView's Gamepad API does not see those, so navigator.getGamepads()
       stays empty and the input just walks focus around the page instead of
       driving. Intercept here and push normalised values into the page. */

    private var padLastX = Float.NaN
    private var padLastRt = Float.NaN
    private var padLastLt = Float.NaN

    private fun isPad(source: Int): Boolean =
        (source and InputDevice.SOURCE_GAMEPAD) == InputDevice.SOURCE_GAMEPAD ||
            (source and InputDevice.SOURCE_JOYSTICK) == InputDevice.SOURCE_JOYSTICK

    override fun dispatchGenericMotionEvent(ev: MotionEvent): Boolean {
        if (isPad(ev.source) && ev.action == MotionEvent.ACTION_MOVE) {
            val x = ev.getAxisValue(MotionEvent.AXIS_X)
            // Pads report triggers on either the trigger or the gas/brake axes.
            val rt = maxOf(
                ev.getAxisValue(MotionEvent.AXIS_RTRIGGER),
                ev.getAxisValue(MotionEvent.AXIS_GAS)
            )
            val lt = maxOf(
                ev.getAxisValue(MotionEvent.AXIS_LTRIGGER),
                ev.getAxisValue(MotionEvent.AXIS_BRAKE)
            )
            pushPad(x, rt, lt)
            return true
        }
        return super.dispatchGenericMotionEvent(ev)
    }

    override fun dispatchKeyEvent(ev: KeyEvent): Boolean {
        if (isPad(ev.source)) {
            val action = when (ev.keyCode) {
                KeyEvent.KEYCODE_BUTTON_B, KeyEvent.KEYCODE_BUTTON_START -> "estop"
                KeyEvent.KEYCODE_BUTTON_A -> "siren"
                KeyEvent.KEYCODE_BUTTON_X -> "lights"
                KeyEvent.KEYCODE_BUTTON_Y -> "wide"
                else -> null
            }
            if (action != null) {
                if (ev.action == KeyEvent.ACTION_DOWN && ev.repeatCount == 0) padAction(action)
                return true   // swallow so the pad never walks page focus
            }
            // Some pads send the triggers as buttons rather than axes.
            if (ev.keyCode == KeyEvent.KEYCODE_BUTTON_R2 || ev.keyCode == KeyEvent.KEYCODE_BUTTON_L2) {
                val on = if (ev.action == KeyEvent.ACTION_DOWN) 1f else 0f
                if (ev.keyCode == KeyEvent.KEYCODE_BUTTON_R2) {
                    pushPad(padLastX.orZero(), on, padLastLt.orZero())
                } else {
                    pushPad(padLastX.orZero(), padLastRt.orZero(), on)
                }
                return true
            }
        }
        return super.dispatchKeyEvent(ev)
    }

    private fun Float.orZero(): Float = if (isNaN()) 0f else this

    /** Quantised to 0.02 so a resting stick does not spam the JS bridge. */
    private fun pushPad(x: Float, rt: Float, lt: Float) {
        val qx = quant(x)
        val qr = quant(rt)
        val ql = quant(lt)
        if (qx == padLastX && qr == padLastRt && ql == padLastLt) return
        padLastX = qx
        padLastRt = qr
        padLastLt = ql
        evalJs("window.padNative && window.padNative($qx,$qr,$ql)")
    }

    private fun quant(v: Float): Float = Math.round(v * 50f) / 50f

    private fun padAction(name: String) {
        evalJs("window.padNativeAction && window.padNativeAction('$name')")
    }

    private fun evalJs(js: String) {
        webView.post { webView.evaluateJavascript(js, null) }
    }

    override fun onDestroy() {
        proxy?.stop()
        io.shutdownNow()
        webView.destroy()
        super.onDestroy()
    }

    private fun hostOnly(espBase: String): String {
        return espBase
            .removePrefix("http://")
            .removePrefix("https://")
            .trimEnd('/')
            .substringBefore('/')
            .substringBefore(':')
    }

    private fun httpProbe(url: String, timeoutMs: Int): Pair<Boolean, String> {
        return try {
            val conn = (URL(url).openConnection() as HttpURLConnection).apply {
                connectTimeout = timeoutMs
                readTimeout = timeoutMs
                instanceFollowRedirects = true
                requestMethod = "GET"
                setRequestProperty("Connection", "close")
            }
            val code = conn.responseCode
            val ok = code in 200..299
            val snippet = try {
                (if (ok) conn.inputStream else conn.errorStream)
                    ?.bufferedReader()?.use { it.readText().take(120) } ?: ""
            } catch (_: Exception) { "" }
            conn.disconnect()
            ok to "HTTP $code $snippet".trim()
        } catch (e: Exception) {
            false to (e.message ?: "error")
        }
    }

    inner class TraceBridge {
        @JavascriptInterface
        fun getMjpegProxyUrl(espBase: String?): String {
            val stream = MjpegProxyServer.streamUrlFromEspBase(
                espBase?.ifBlank { null } ?: DEFAULT_ESP_BASE
            )
            proxy?.updateUpstream(stream)
            val port = proxy?.localPort ?: return stream
            return "http://127.0.0.1:$port/stream"
        }

        @JavascriptInterface
        fun getDefaultEsp(): String = DEFAULT_ESP_BASE

        @JavascriptInterface
        fun getDefaultBrain(): String = DEFAULT_BRAIN

        /** Sync probe for Connect button — returns JSON string. */
        @JavascriptInterface
        fun probeEsp(espBase: String?): String {
            val base = (espBase?.ifBlank { null } ?: DEFAULT_ESP_BASE).trim().trimEnd('/')
            val host = hostOnly(base)
            val statusUrl = "http://$host:8765/api/status"
            val streamUrl = "http://$host:82/stream"
            val (statusOk, statusDetail) = httpProbe(statusUrl, 2500)
            // Stream: just open connection / read a few bytes (MJPEG never "ends")
            val (streamOk, streamDetail) = try {
                val conn = (URL(streamUrl).openConnection() as HttpURLConnection).apply {
                    connectTimeout = 3000
                    readTimeout = 3000
                    requestMethod = "GET"
                    setRequestProperty("Accept", "multipart/x-mixed-replace,*/*")
                }
                val code = conn.responseCode
                val ok = code in 200..299
                if (ok) {
                    try { conn.inputStream.read(ByteArray(64)) } catch (_: Exception) {}
                }
                conn.disconnect()
                ok to "HTTP $code"
            } catch (e: Exception) {
                false to (e.message ?: "error")
            }
            proxy?.updateUpstream(streamUrl)
            return JSONObject()
                .put("ok", statusOk && streamOk)
                .put("host", host)
                .put("statusOk", statusOk)
                .put("statusDetail", statusDetail)
                .put("statusUrl", statusUrl)
                .put("streamOk", streamOk)
                .put("streamDetail", streamDetail)
                .put("streamUrl", streamUrl)
                .put("wifiOk", isOnWifi())
                .toString()
        }

        @JavascriptInterface
        fun isOnWifi(): Boolean {
            return try {
                val cm = getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
                val net = cm.activeNetwork ?: return false
                val caps = cm.getNetworkCapabilities(net) ?: return false
                caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)
            } catch (_: Exception) {
                false
            }
        }

        @JavascriptInterface
        fun setFullscreen(on: Boolean) {
            runOnUiThread {
                requestedOrientation = if (on) {
                    ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE
                } else {
                    ActivityInfo.SCREEN_ORIENTATION_SENSOR_PORTRAIT
                }
            }
        }
    }

    companion object {
        const val DEFAULT_ESP_BASE = "http://192.168.1.104"
        const val DEFAULT_ESP_STREAM = "http://192.168.1.104:82/stream"
        /** PC running desktop/speak_server.py — editable in UI (never 127.0.0.1 on tablet). */
        const val DEFAULT_BRAIN = "http://192.168.1.108:8799"
    }
}
