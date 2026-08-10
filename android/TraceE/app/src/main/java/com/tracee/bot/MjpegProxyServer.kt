package com.tracee.bot

import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.net.HttpURLConnection
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.URL
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

/**
 * Tiny localhost MJPEG proxy using HttpURLConnection.
 * WebView loads http://127.0.0.1:<port>/stream while this class pulls
 * from the ESP (e.g. http://192.168.1.104:82/stream) without CORS pain.
 */
class MjpegProxyServer(
    defaultEspStream: String,
    private val bindPort: Int = 0 // 0 = ephemeral
) {
    private val upstream = AtomicReference(defaultEspStream)
    private val running = AtomicBoolean(false)
    private var server: ServerSocket? = null
    private val pool = Executors.newCachedThreadPool()

    val localPort: Int
        get() = server?.localPort ?: -1

    fun updateUpstream(url: String) {
        upstream.set(url.trim().trimEnd('/'))
    }

    fun start() {
        if (running.getAndSet(true)) return
        server = ServerSocket(bindPort, 8, InetAddress.getByName("127.0.0.1"))
        pool.execute {
            while (running.get()) {
                try {
                    val client = server?.accept() ?: break
                    pool.execute { handleClient(client) }
                } catch (_: Exception) {
                    if (!running.get()) break
                }
            }
        }
    }

    fun stop() {
        running.set(false)
        try { server?.close() } catch (_: Exception) {}
        pool.shutdownNow()
    }

    private fun handleClient(socket: Socket) {
        try {
            socket.soTimeout = 15000
            val input = BufferedInputStream(socket.getInputStream())
            val output = BufferedOutputStream(socket.getOutputStream())
            // Read request line (ignore rest for scaffold)
            val req = readLineAscii(input) ?: return
            if (!req.contains("GET")) {
                writeHttp(output, 405, "text/plain", "Method not allowed")
                return
            }
            if (!req.contains("/stream")) {
                writeHttp(output, 404, "text/plain", "Try /stream")
                return
            }

            val src = URL(upstream.get())
            val conn = (src.openConnection() as HttpURLConnection).apply {
                connectTimeout = 4000
                readTimeout = 0 // streaming
                instanceFollowRedirects = true
                requestMethod = "GET"
                setRequestProperty("Accept", "multipart/x-mixed-replace,*/*")
                setRequestProperty("Connection", "close")
            }
            val code = conn.responseCode
            if (code !in 200..299) {
                writeHttp(output, 502, "text/plain", "Upstream $code")
                conn.disconnect()
                return
            }
            val ctype = conn.contentType ?: "multipart/x-mixed-replace; boundary=frame"
            val header = buildString {
                append("HTTP/1.1 200 OK\r\n")
                append("Content-Type: ").append(ctype).append("\r\n")
                append("Cache-Control: no-cache, no-store\r\n")
                append("Pragma: no-cache\r\n")
                append("Connection: close\r\n")
                append("Access-Control-Allow-Origin: *\r\n")
                append("\r\n")
            }
            output.write(header.toByteArray(Charsets.US_ASCII))
            output.flush()

            val up = BufferedInputStream(conn.inputStream)
            val buf = ByteArray(16 * 1024)
            while (running.get()) {
                val n = up.read(buf)
                if (n < 0) break
                output.write(buf, 0, n)
                output.flush()
            }
            conn.disconnect()
        } catch (_: Exception) {
            // client gone / ESP offline — fine for scaffold
        } finally {
            try { socket.close() } catch (_: Exception) {}
        }
    }

    private fun writeHttp(out: BufferedOutputStream, code: Int, type: String, body: String) {
        val bytes = body.toByteArray(Charsets.UTF_8)
        val h = "HTTP/1.1 $code OK\r\nContent-Type: $type\r\nContent-Length: ${bytes.size}\r\nConnection: close\r\n\r\n"
        out.write(h.toByteArray(Charsets.US_ASCII))
        out.write(bytes)
        out.flush()
    }

    private fun readLineAscii(input: BufferedInputStream): String? {
        val sb = StringBuilder()
        while (true) {
            val c = input.read()
            if (c < 0) return if (sb.isEmpty()) null else sb.toString()
            if (c == '\n'.code) break
            if (c != '\r'.code) sb.append(c.toChar())
        }
        return sb.toString()
    }

    companion object {
        fun streamUrlFromEspBase(espBase: String): String {
            val host = espBase
                .removePrefix("http://")
                .removePrefix("https://")
                .trimEnd('/')
                .substringBefore('/')
                .substringBefore(':')
            return "http://$host:82/stream"
        }
    }
}
