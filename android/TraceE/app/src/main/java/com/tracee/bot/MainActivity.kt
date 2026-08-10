package com.tracee.bot

import android.annotation.SuppressLint
import android.os.Bundle
import android.webkit.JavascriptInterface
import android.webkit.WebChromeClient
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.appcompat.app.AppCompatActivity

/**
 * Trace-E WEB-Quarters shell: loads local assets/www control UI.
 * MJPEG is served through [MjpegProxyServer] so WebView gets a stable
 * same-origin stream URL instead of stalling on ESP multipart.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private var proxy: MjpegProxyServer? = null

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        webView = WebView(this)
        setContentView(webView)

        proxy = MjpegProxyServer(defaultEspStream = DEFAULT_ESP_STREAM).also { it.start() }

        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false
            mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
            cacheMode = WebSettings.LOAD_DEFAULT
            allowFileAccess = true
            // Needed so file:// HQ page can call LAN brain / proxy
            allowUniversalAccessFromFileURLs = true
            allowFileAccessFromFileURLs = true
        }
        webView.webChromeClient = WebChromeClient()
        webView.webViewClient = WebViewClient()
        webView.addJavascriptInterface(TraceBridge(), "TraceEAndroid")

        // Mobile HQ page (copy of desktop android_mock / slim HQ)
        webView.loadUrl("file:///android_asset/www/index.html")
    }

    override fun onDestroy() {
        proxy?.stop()
        webView.destroy()
        super.onDestroy()
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
    }

    companion object {
        /** Match current Trace / peanut LAN defaults — editable in UI. */
        const val DEFAULT_ESP_BASE = "http://192.168.1.104"
        const val DEFAULT_ESP_STREAM = "http://192.168.1.104:82/stream"
        /** PC running desktop/speak_server.py — set to your PC LAN IP when testing. */
        const val DEFAULT_BRAIN = "http://192.168.1.100:8787"
    }
}
