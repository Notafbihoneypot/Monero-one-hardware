package one.monero.moneroone.ui.screens.keystone

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.QrCodeScanner
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch
import one.monero.moneroone.core.wallet.KeystonePairing
import one.monero.moneroone.core.wallet.WalletViewModel
import one.monero.moneroone.core.wallet.parseKeystonePairing
import one.monero.moneroone.core.wallet.parseKeystonePairingText
import one.monero.moneroone.ui.components.AddWalletFlowEffect
import one.monero.moneroone.ui.components.GlassCard
import one.monero.moneroone.ui.components.PrimaryButton
import one.monero.moneroone.ui.theme.truncateMiddle

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun KeystonePairScreen(
    walletViewModel: WalletViewModel,
    flowId: String,
    isAddingWallet: Boolean,
    onPaired: () -> Unit,
    onBack: () -> Unit
) {
    AddWalletFlowEffect(walletViewModel)
    val scope = rememberCoroutineScope()
    val walletState by walletViewModel.walletState.collectAsState()

    var scanning by remember { mutableStateOf(false) }
    var pairing by remember { mutableStateOf<KeystonePairing?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Pair Keystone") },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back")
                    }
                }
            )
        }
    ) { padding ->
        Box(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
        ) {
            if (scanning) {
                Column(modifier = Modifier.fillMaxSize()) {
                    Text(
                        "Scan the Feather/Monero wallet QR shown by Keystone.",
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(16.dp),
                        textAlign = TextAlign.Center
                    )
                    KeystoneQrScanner(
                        allowText = true,
                        modifier = Modifier
                            .fillMaxWidth()
                            .weight(1f),
                        onDecoded = { payload ->
                            try {
                                pairing = when (payload) {
                                    is KeystoneScanPayload.Text -> parseKeystonePairingText(payload.value)
                                    is KeystoneScanPayload.Ur -> parseKeystonePairing(payload.bytes)
                                }
                                error = null
                                scanning = false
                            } catch (e: Exception) {
                                error = e.message ?: "Invalid Keystone pairing QR"
                            }
                        },
                        onError = { error = it }
                    )
                }
            } else {
                Column(
                    modifier = Modifier
                        .fillMaxSize()
                        .padding(horizontal = 24.dp),
                    horizontalAlignment = Alignment.CenterHorizontally,
                    verticalArrangement = Arrangement.Center
                ) {
                    Text(
                        "Keystone stays the signer",
                        style = MaterialTheme.typography.headlineSmall,
                        fontWeight = FontWeight.SemiBold,
                        textAlign = TextAlign.Center
                    )
                    Spacer(Modifier.height(12.dp))
                    Text(
                        "Monero One stores only the primary address and private view key. The recovery phrase and private spend key stay on Keystone.",
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        textAlign = TextAlign.Center
                    )
                    Spacer(Modifier.height(24.dp))

                    GlassCard(modifier = Modifier.fillMaxWidth()) {
                        Column(Modifier.padding(18.dp)) {
                            Text(
                                "On Keystone",
                                style = MaterialTheme.typography.titleMedium,
                                fontWeight = FontWeight.SemiBold
                            )
                            Spacer(Modifier.height(8.dp))
                            Text(
                                "Open Monero, choose the Feather connection, and display the wallet QR. Monero One reads the same view-only QR Feather accepts."
                            )
                        }
                    }

                    Spacer(Modifier.height(18.dp))

                    val current = pairing
                    if (current == null) {
                        PrimaryButton(
                            onClick = { error = null; scanning = true },
                            modifier = Modifier.fillMaxWidth()
                        ) {
                            Icon(Icons.Default.QrCodeScanner, contentDescription = null)
                            Text("Scan Keystone")
                        }
                    } else {
                        GlassCard(modifier = Modifier.fillMaxWidth()) {
                            Column(Modifier.padding(18.dp)) {
                                Text(current.walletName, style = MaterialTheme.typography.titleMedium)
                                Spacer(Modifier.height(8.dp))
                                Text(
                                    truncateMiddle(current.primaryAddress),
                                    style = MaterialTheme.typography.bodySmall
                                )
                                Text(
                                    "Restore height: " + current.restoreHeight,
                                    style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant
                                )
                            }
                        }
                        Spacer(Modifier.height(14.dp))
                        PrimaryButton(
                            onClick = {
                                busy = true
                                error = null
                                scope.launch {
                                    val added = walletViewModel.addKeystoneWallet(
                                        pairing = current,
                                        flowId = flowId,
                                        name = current.walletName
                                    )
                                    busy = false
                                    if (added) onPaired()
                                    else error = walletState.error ?: "Could not add Keystone wallet"
                                }
                            },
                            modifier = Modifier.fillMaxWidth(),
                            enabled = !busy
                        ) {
                            Text(if (busy) "Adding..." else if (isAddingWallet) "Add Keystone Wallet" else "Use Keystone Wallet")
                        }
                        Spacer(Modifier.height(10.dp))
                        androidx.compose.material3.TextButton(
                            onClick = { pairing = null; scanning = true },
                            enabled = !busy
                        ) { Text("Scan again") }
                    }

                    if (error != null) {
                        Spacer(Modifier.height(14.dp))
                        Text(
                            error.orEmpty(),
                            color = MaterialTheme.colorScheme.error,
                            textAlign = TextAlign.Center
                        )
                    }
                }
            }
        }
    }
}
