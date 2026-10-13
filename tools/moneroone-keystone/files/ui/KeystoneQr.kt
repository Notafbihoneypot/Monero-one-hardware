package one.monero.moneroone.ui.screens.keystone

import android.Manifest
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.view.ViewGroup
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.camera.core.CameraSelector
import androidx.camera.core.ExperimentalGetImage
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import com.google.mlkit.vision.barcode.BarcodeScanning
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.common.InputImage
import com.google.zxing.BarcodeFormat
import com.google.zxing.qrcode.QRCodeWriter
import com.sparrowwallet.hummingbird.ResultType
import com.sparrowwallet.hummingbird.UR
import com.sparrowwallet.hummingbird.URDecoder
import com.sparrowwallet.hummingbird.UREncoder
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

sealed interface KeystoneScanPayload {
    data class Text(val value: String) : KeystoneScanPayload
    data class Ur(val type: String, val bytes: ByteArray) : KeystoneScanPayload
}

@Composable
fun AnimatedKeystoneUr(
    type: String,
    data: ByteArray,
    modifier: Modifier = Modifier,
    fps: Int = 5
) {
    // ByteArray.contentHashCode() is O(payload size). Computing it directly in
    // Compose keys meant every animated-frame recomposition re-hashed the full
    // Monero output/transaction payload. Cache it for this payload instance.
    val dataHash = remember(data) { data.contentHashCode() }
    var bitmap by remember(type, dataHash) {
        mutableStateOf<androidx.compose.ui.graphics.ImageBitmap?>(null)
    }

    // Generate the UR part and its bitmap in ONE sequential coroutine. The old
    // two-stage frame -> LaunchedEffect(text) pipeline could queue/cancel QR
    // renders faster than QRCodeWriter finished them, causing CPU spikes, GC and
    // visible jank. A new frame is not requested until the previous bitmap is ready.
    LaunchedEffect(type, dataHash, fps) {
        val ur = UR.fromBytes(type, data)
        val encoder = UREncoder(ur, 60, 10, 0)
        val frameDelay = 1000L / fps.coerceIn(2, 10)
        while (true) {
            val text = encoder.nextPart().toString()
            val next = withContext(Dispatchers.Default) {
                makeQrBitmap(text, 512).asImageBitmap()
            }
            bitmap = next
            delay(frameDelay)
        }
    }

    Box(
        modifier = modifier.background(androidx.compose.ui.graphics.Color.White),
        contentAlignment = Alignment.Center
    ) {
        val shown = bitmap
        if (shown == null) {
            CircularProgressIndicator()
        } else {
            Image(
                bitmap = shown,
                contentDescription = "Animated Keystone QR",
                modifier = Modifier.fillMaxSize()
            )
        }
    }
}

private fun makeQrBitmap(text: String, size: Int): Bitmap {
    val matrix = QRCodeWriter().encode(text, BarcodeFormat.QR_CODE, size, size)
    val pixels = IntArray(size * size)
    var offset = 0
    for (y in 0 until size) {
        for (x in 0 until size) {
            pixels[offset++] = if (matrix[x, y]) 0xFF000000.toInt() else 0xFFFFFFFF.toInt()
        }
    }
    return Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888).apply {
        setPixels(pixels, 0, size, 0, 0, size, size)
    }
}

private class KeystoneQrAnalyzer(
    private val onText: (String) -> Unit
) : ImageAnalysis.Analyzer, AutoCloseable {
    private val processing = AtomicBoolean(false)
    private val closed = AtomicBoolean(false)
    private val scanner = BarcodeScanning.getClient(
        com.google.mlkit.vision.barcode.BarcodeScannerOptions.Builder()
            .setBarcodeFormats(Barcode.FORMAT_QR_CODE)
            .build()
    )

    @OptIn(ExperimentalGetImage::class)
    override fun analyze(imageProxy: ImageProxy) {
        if (closed.get() || !processing.compareAndSet(false, true)) {
            imageProxy.close()
            return
        }
        val mediaImage = imageProxy.image
        if (mediaImage == null) {
            processing.set(false)
            imageProxy.close()
            return
        }
        val image = InputImage.fromMediaImage(mediaImage, imageProxy.imageInfo.rotationDegrees)
        scanner.process(image)
            .addOnSuccessListener { barcodes ->
                if (!closed.get()) barcodes.firstOrNull()?.rawValue?.let(onText)
            }
            .addOnCompleteListener {
                processing.set(false)
                imageProxy.close()
            }
    }

    override fun close() {
        if (closed.compareAndSet(false, true)) {
            scanner.close()
        }
    }
}

@Composable
private fun KeystoneCamera(
    onText: (String) -> Unit,
    modifier: Modifier = Modifier
) {
    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current
    val executor = remember { Executors.newSingleThreadExecutor() }
    val providerRef = remember { AtomicReference<ProcessCameraProvider?>(null) }
    val analyzerRef = remember { AtomicReference<KeystoneQrAnalyzer?>(null) }
    val onTextRef = remember { AtomicReference(onText) }
    val disposed = remember { AtomicBoolean(false) }
    onTextRef.set(onText)

    DisposableEffect(Unit) {
        onDispose {
            disposed.set(true)
            analyzerRef.getAndSet(null)?.close()
            providerRef.getAndSet(null)?.unbindAll()
            executor.shutdownNow()
        }
    }

    AndroidView(
        modifier = modifier,
        factory = { ctx ->
            val previewView = PreviewView(ctx).apply {
                layoutParams = ViewGroup.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.MATCH_PARENT
                )
                scaleType = PreviewView.ScaleType.FILL_CENTER
            }
            val future = ProcessCameraProvider.getInstance(ctx)
            future.addListener({
                val provider = future.get()
                if (disposed.get()) {
                    provider.unbindAll()
                    return@addListener
                }
                providerRef.set(provider)
                val preview = Preview.Builder().build().also {
                    it.setSurfaceProvider(previewView.surfaceProvider)
                }
                val analyzer = KeystoneQrAnalyzer { raw -> onTextRef.get().invoke(raw) }
                analyzerRef.set(analyzer)
                val analysis = ImageAnalysis.Builder()
                    .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                    .build()
                    .also { it.setAnalyzer(executor, analyzer) }
                provider.unbindAll()
                provider.bindToLifecycle(
                    lifecycleOwner,
                    CameraSelector.DEFAULT_BACK_CAMERA,
                    preview,
                    analysis
                )
            }, ContextCompat.getMainExecutor(ctx))
            previewView
        }
    )
}

@Composable
fun KeystoneQrScanner(
    expectedType: String? = null,
    allowText: Boolean = false,
    onDecoded: (KeystoneScanPayload) -> Unit,
    onError: (String) -> Unit,
    modifier: Modifier = Modifier
) {
    val context = LocalContext.current
    var permission by remember {
        mutableStateOf(
            ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) ==
                PackageManager.PERMISSION_GRANTED
        )
    }
    val decoderRef = remember { AtomicReference(URDecoder()) }
    val lastFrameRef = remember { AtomicReference<String?>(null) }
    var progress by remember { mutableStateOf(0f) }
    var complete by remember { mutableStateOf(false) }

    val permissionLauncher = rememberLauncherForActivityResult(
        androidx.activity.result.contract.ActivityResultContracts.RequestPermission()
    ) { permission = it }

    if (!permission) {
        Column(
            modifier = modifier.padding(24.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center
        ) {
            Text("Camera access is required to scan Keystone QR codes.", textAlign = TextAlign.Center)
            androidx.compose.material3.TextButton(
                onClick = { permissionLauncher.launch(Manifest.permission.CAMERA) }
            ) { Text("Allow camera") }
        }
        return
    }

    Box(modifier = modifier) {
        KeystoneCamera(
            modifier = Modifier.fillMaxSize(),
            onText = scan@{ raw ->
                if (complete || raw == lastFrameRef.get()) return@scan
                lastFrameRef.set(raw)
                try {
                    if (raw.startsWith("ur:", ignoreCase = true)) {
                        val decoder = decoderRef.get()
                        decoder.receivePart(raw)
                        val nextProgress = decoder.estimatedPercentComplete.toFloat().coerceIn(0f, 1f)
                        if (kotlin.math.abs(nextProgress - progress) >= 0.01f || nextProgress >= 1f) {
                            progress = nextProgress
                        }
                        val result = decoder.result
                        if (result != null && result.type == ResultType.SUCCESS) {
                            val ur = result.ur
                            if (expectedType != null && !ur.type.equals(expectedType, ignoreCase = true)) {
                                onError("Expected " + expectedType + " but scanned " + ur.type)
                                decoderRef.set(URDecoder())
                                progress = 0f
                                lastFrameRef.set(null)
                                return@scan
                            }
                            complete = true
                            onDecoded(KeystoneScanPayload.Ur(ur.type, ur.toBytes()))
                        }
                    } else if (allowText) {
                        complete = true
                        onDecoded(KeystoneScanPayload.Text(raw))
                    }
                } catch (e: Exception) {
                    onError(e.message ?: "Could not decode QR")
                    decoderRef.set(URDecoder())
                    progress = 0f
                    lastFrameRef.set(null)
                }
            }
        )

        if (progress > 0f && !complete) {
            Column(
                modifier = Modifier
                    .align(Alignment.BottomCenter)
                    .padding(24.dp)
                    .background(MaterialTheme.colorScheme.surface.copy(alpha = 0.9f))
                    .padding(12.dp)
                    .fillMaxWidth(),
                horizontalAlignment = Alignment.CenterHorizontally
            ) {
                CircularProgressIndicator(progress = { progress }, modifier = Modifier.size(36.dp))
                Text(
                    text = "QR " + (progress * 100).toInt() + "%",
                    style = MaterialTheme.typography.bodySmall,
                    modifier = Modifier.padding(top = 6.dp)
                )
            }
        }
    }
}
