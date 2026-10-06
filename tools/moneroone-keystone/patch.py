#!/usr/bin/env python3
from pathlib import Path
import re
import shutil
import sys

root = Path(sys.argv[1]).resolve()
assets = Path(__file__).resolve().parent / "files"

def read(rel):
    return (root / rel).read_text()

def write(rel, data):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)

def rep(rel, old, new, count=1):
    data = read(rel)
    if old not in data:
        raise SystemExit("patch anchor missing in " + rel + ": " + old[:120])
    write(rel, data.replace(old, new, count))

def insert_before(rel, marker, addition):
    data = read(rel)
    if marker not in data:
        raise SystemExit("insert anchor missing in " + rel + ": " + marker[:120])
    write(rel, data.replace(marker, addition + marker, 1))

def copy_asset(src, dst):
    target = root / dst
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(assets / src, target)

# ---------------------------------------------------------------------------
# App dependency: BC-UR / Fountain transport used by Feather-compatible QR.
# ---------------------------------------------------------------------------
rep(
    "app/build.gradle.kts",
    "    implementation(libs.qrcode.kotlin)\n",
    "    implementation(libs.qrcode.kotlin)\n    implementation(\"com.sparrowwallet:hummingbird:1.7.4\")\n"
)

# ---------------------------------------------------------------------------
# Fast-sync hardening.
#
# A freshly paired hardware/view-only wallet can scan millions of blocks. The
# upstream kit currently serializes the wallet cache every 2,000 blocks and
# rebuilds transaction history every ~2 seconds while catching up. On Android
# that creates heavy disk I/O, allocation/GC churn and Compose jank.
#
# Also enforce the requested restore height for watch-only wallets. wallet2 can
# reopen a partially-created view-only cache with restoreHeight=0 even though
# MoneroKit was initialized with a later restore height.
# ---------------------------------------------------------------------------
monero_kit = "monero-kit-android/monerokit/src/main/java/io/horizontalsystems/monerokit/MoneroKit.kt"

# Capture the identity wallet2 actually opens before any daemon operation can
# fail. Direct Keystone pairing uses these values as the authoritative check.
rep(
    monero_kit,
r'''    @Volatile
    var checkedWalletFilePrimaryAddress: String? = null
        private set
''',
r'''    @Volatile
    var checkedWalletFilePrimaryAddress: String? = null
        private set

    @Volatile
    var checkedWalletFilePrivateViewKey: String? = null
        private set
'''
)
rep(
    monero_kit,
r'''        checkedWalletFilePrimaryAddress = null
        try {
''',
r'''        checkedWalletFilePrimaryAddress = null
        checkedWalletFilePrivateViewKey = null
        try {
'''
)
rep(
    monero_kit,
r'''            checkedWalletFilePrimaryAddress = walletService.withWallet { it.getSubaddress(0, 0) }
''',
r'''            checkedWalletFilePrimaryAddress = walletService.withWallet { it.getSubaddress(0, 0) }
            checkedWalletFilePrivateViewKey = walletService.withWallet { it.secretViewKey }
'''
)
rep(
    monero_kit,
    "    private var lastStoreHeight: Long = 0\n",
    "    private var lastStoreHeight: Long = 0\n    private var lastStoreTimeMs: Long = 0\n"
)
rep(
    monero_kit,
r'''        val historyAll: List<TransactionInfo?>? = wallet.history.all

        if (historyAll != null) {
            _allTransactionsFlow.update {
                historyAll.mapNotNull { it }
            }
        }
''',
r'''        // Reading and mapping the entire history on every sync callback creates
        // substantial allocation/GC pressure while scanning. Only refresh the public
        // transaction list when the listener detected a history change or at final sync.
        if (full || wallet.isSynchronized) {
            val historyAll: List<TransactionInfo?>? = wallet.history.all
            if (historyAll != null) {
                _allTransactionsFlow.update {
                    historyAll.mapNotNull { it }
                }
            }
        }
''')
rep(
    monero_kit,
r'''            // Periodically store wallet state during sync (every 2000 blocks)
            if (walletHeight - lastStoreHeight >= 2000) {
                if (!savingState.getAndSet(true)) {
                    walletService.storeWallet()
                    savingState.set(false)
                    lastStoreHeight = walletHeight
                }
            }
''',
r'''            // Persist enough progress to survive interruption without serializing a
            // multi-megabyte wallet every couple of seconds on a fast initial scan.
            val now = System.currentTimeMillis()
            if (walletHeight - lastStoreHeight >= 25_000 && now - lastStoreTimeMs >= 15_000) {
                if (!savingState.getAndSet(true)) {
                    walletService.storeWallet()
                    savingState.set(false)
                    lastStoreHeight = walletHeight
                    lastStoreTimeMs = now
                }
            }
''')
rep(
    monero_kit,
r'''            is Seed.WatchOnly -> {
                val newWallet = WalletManager.getInstance().createWalletWithKeys(
                    /* aFile = */ newWalletFile,
                    /* password = */ walletPassword,
                    /* language = */ "",
                    /* restoreHeight = */ creationHeight,
                    /* addressString = */ seed.address,
                    /* viewKeyString = */ seed.viewPrivateKey,
                    /* spendKeyString = */ ""
                )

                checkAndCloseWallet(newWallet)
            }
''',
r'''            is Seed.WatchOnly -> {
                val newWallet = WalletManager.getInstance().createWalletWithKeys(
                    /* aFile = */ newWalletFile,
                    /* password = */ walletPassword,
                    /* language = */ "",
                    /* restoreHeight = */ creationHeight,
                    /* addressString = */ seed.address,
                    /* viewKeyString = */ seed.viewPrivateKey,
                    /* spendKeyString = */ ""
                )

                // createWalletFromKeys may report/persist a zero restore height for
                // view-only wallets. Set it explicitly before the keys file is closed.
                newWallet.setRestoreHeight(creationHeight)
                checkAndCloseWallet(newWallet)
            }
''')

wallet_service_fast = "monero-kit-android/monerokit/src/main/java/io/horizontalsystems/monerokit/WalletService.kt"
rep(
    wallet_service_fast,
r'''        if (restoreHeight != null) {
            val height = wallet.blockChainHeight
            if (height <= 1) {
                val from = restoreHeight.coerceAtLeast(0)
                val initFrom = wallet.restoreHeight
                if (initFrom != from) Timber.w("wallet at height %d: scan from %d, not %d", height, from, initFrom)
                wallet.setRestoreHeight(from)
            }
        }
''',
r'''        if (restoreHeight != null) {
            val height = wallet.blockChainHeight
            val from = restoreHeight.coerceAtLeast(0)
            val initFrom = wallet.restoreHeight
            // A partially scanned view-only cache can reopen with restoreHeight=0.
            // If it is still below the requested scan start, enforce the caller's
            // restore height instead of continuing an unnecessary genesis scan.
            if (initFrom != from && (height <= 1 || height < from)) {
                Timber.w("wallet at height %d: scan from %d, not %d", height, from, initFrom)
                wallet.setRestoreHeight(from)
            }
        }
''')
rep(
    wallet_service_fast,
r'''        private var lastBlockTime = 0L
        private var lastTxCount = 0
''',
r'''        private var lastBlockTime = 0L
        private var lastHistoryRefreshTime = 0L
        private var lastTxCount = 0
''')
rep(
    wallet_service_fast,
r'''            if (lastBlockTime < System.currentTimeMillis() - 2000) {
                lastBlockTime = System.currentTimeMillis()
''',
r'''            if (lastBlockTime < System.currentTimeMillis() - 4000) {
                lastBlockTime = System.currentTimeMillis()
''')
# Older monero-kit revisions refreshed history inside an explicit
# if (!wallet.isSynchronized) block. Newer revisions moved that logic into a
# different listener shape. Apply the old throttling patch only when that exact
# layout is present; otherwise keep upstream's current listener semantics.
_old_history_listener = r'''                    if (!wallet.isSynchronized) {
                        updated = true
                        // we want to see our transactions as they come in
                        wallet.refreshHistory()
                        val txCount = wallet.getHistory().getCount()
                        if (txCount > lastTxCount) {
                            // update the transaction list only if we have more than before
                            lastTxCount = txCount
                            fullRefresh = true
                        }
                    }
                    observer?.onRefreshed(wallet, Status(), fullRefresh)
'''
_new_history_listener = r'''                    if (!wallet.isSynchronized) {
                        // History refresh is comparatively expensive and previously ran every
                        // sync callback. Refresh immediately for a wallet2 "updated" event and
                        // otherwise only periodically while bulk scanning.
                        val now = System.currentTimeMillis()
                        if (updated || now - lastHistoryRefreshTime >= 15_000) {
                            wallet.refreshHistory()
                            lastHistoryRefreshTime = now
                            updated = false
                            val txCount = wallet.getHistory().getCount()
                            if (txCount != lastTxCount) {
                                lastTxCount = txCount
                                fullRefresh = true
                            }
                        }
                    }
                    observer?.onRefreshed(wallet, Status(), fullRefresh)
'''
_ws_data = read(wallet_service_fast)
if _old_history_listener in _ws_data:
    write(wallet_service_fast, _ws_data.replace(_old_history_listener, _new_history_listener, 1))
else:
    print("WalletService listener changed upstream; keeping current history-refresh logic")

# ---------------------------------------------------------------------------
# Native Monero wallet2 bridge: expose only the file operations needed by the
# online watch-only wallet. Signing remains on Keystone.
# ---------------------------------------------------------------------------
wallet_java = "monero-kit-android/monerokit/src/main/java/io/horizontalsystems/monerokit/model/Wallet.java"
rep(
    wallet_java,
    "//virtual UnsignedTransaction * loadUnsignedTx(const std::string &unsigned_filename) = 0;\n//virtual bool submitTransaction(const std::string &fileName) = 0;\n",
    "public native boolean submitTransaction(String fileName);\n"
)
rep(
    wallet_java,
    "    //virtual bool exportKeyImages(const std::string &filename) = 0;\n//virtual bool importKeyImages(const std::string &filename) = 0;\n",
    "    public native boolean exportOutputs(String filename, boolean all);\n    public native boolean importKeyImages(String filename);\n"
)

cpp = "monero-kit-android/monerokit/src/main/cpp/monerujo.cpp"
rep(
    cpp,
    "//virtual UnsignedTransaction * loadUnsignedTx(const std::string &unsigned_filename) = 0;\n//virtual bool submitTransaction(const std::string &fileName) = 0;\n",
r'''JNIEXPORT jboolean JNICALL
Java_io_horizontalsystems_monerokit_model_Wallet_submitTransaction(
        JNIEnv *env, jobject instance, jstring fileName) {
    const char *_fileName = env->GetStringUTFChars(fileName, nullptr);
    if (_fileName == nullptr) return JNI_FALSE;
    Monero::Wallet *wallet = getHandle<Monero::Wallet>(env, instance);
    bool success = false;
    try {
        success = wallet->submitTransaction(std::string(_fileName));
    } catch (const std::exception &e) {
        throwIllegalState(env, std::string("Signed transaction submit failed: ") + e.what());
    } catch (...) {
        throwIllegalState(env, "Signed transaction submit failed");
    }
    env->ReleaseStringUTFChars(fileName, _fileName);
    return static_cast<jboolean>(success);
}

''')
rep(
    cpp,
    "//virtual bool exportKeyImages(const std::string &filename) = 0;\n//virtual bool importKeyImages(const std::string &filename) = 0;\n",
r'''JNIEXPORT jboolean JNICALL
Java_io_horizontalsystems_monerokit_model_Wallet_exportOutputs(
        JNIEnv *env, jobject instance, jstring filename, jboolean all) {
    const char *_filename = env->GetStringUTFChars(filename, nullptr);
    if (_filename == nullptr) return JNI_FALSE;
    Monero::Wallet *wallet = getHandle<Monero::Wallet>(env, instance);
    bool success = false;
    try {
        success = wallet->exportOutputs(std::string(_filename), static_cast<bool>(all));
    } catch (const std::exception &e) {
        throwIllegalState(env, std::string("Output export failed: ") + e.what());
    } catch (...) {
        throwIllegalState(env, "Output export failed");
    }
    env->ReleaseStringUTFChars(filename, _filename);
    return static_cast<jboolean>(success);
}

JNIEXPORT jboolean JNICALL
Java_io_horizontalsystems_monerokit_model_Wallet_importKeyImages(
        JNIEnv *env, jobject instance, jstring filename) {
    const char *_filename = env->GetStringUTFChars(filename, nullptr);
    if (_filename == nullptr) return JNI_FALSE;
    Monero::Wallet *wallet = getHandle<Monero::Wallet>(env, instance);
    bool success = false;
    try {
        success = wallet->importKeyImages(std::string(_filename));
    } catch (const std::exception &e) {
        throwIllegalState(env, std::string("Key image import failed: ") + e.what());
    } catch (...) {
        throwIllegalState(env, "Key image import failed");
    }
    env->ReleaseStringUTFChars(filename, _filename);
    return static_cast<jboolean>(success);
}

''')

service = "monero-kit-android/monerokit/src/main/java/io/horizontalsystems/monerokit/WalletService.kt"
insert_before(
    service,
    "    /** Creates and commits one transaction, both on the same wallet. */\n",
r'''    fun exportOutputs(filename: String, all: Boolean = true): Boolean =
        withSession { wallet ->
            atRest(wallet) { wallet.exportOutputs(filename, all) }
        } ?: throw IllegalStateException("Wallet is NULL")

    /**
     * Runs a wallet operation with background refresh stopped but daemon RPCs still
     * available. atRest() deliberately sets wallet2 offline while draining a refresh
     * pass; these hardware-signing operations need the daemon, so bring it online
     * only after the refresh thread is quiescent. atRest() resumes refresh afterward.
     */
    private inline fun <T> atRestOnline(wallet: Wallet, block: () -> T): T =
        atRest(wallet) {
            wallet.setOffline(false)
            block()
        }

    fun importKeyImages(filename: String): Boolean =
        withSession { wallet ->
            atRestOnline(wallet) {
                // Monero's WalletImpl::importKeyImages refuses to run unless the
                // daemon is marked trusted because it asks the daemon for spent/
                // unspent status. Trust only this operation, then restore the
                // caller's previous setting.
                val wasTrusted = wallet.trustedDaemon()
                try {
                    wallet.setTrustedDaemon(true)
                    val imported = wallet.importKeyImages(filename)
                    if (!imported) {
                        val reason = wallet.status.errorString
                            .takeIf { !it.isNullOrBlank() }
                            ?: "wallet2 rejected the key-image file"
                        Timber.e("Keystone key-image import failed: %s", reason)
                        throw IllegalStateException("Key image sync failed: " + reason)
                    }
                    Timber.i("Keystone key-image import succeeded")
                    wallet.refreshHistory()
                    listener?.updated = true
                    storeCache(wallet)
                    true
                } finally {
                    wallet.setTrustedDaemon(wasTrusted)
                }
            }
        } ?: throw IllegalStateException("Wallet is NULL")

    /**
     * A watch-only wallet creates the transaction set but saves it instead of
     * broadcasting. wallet2 writes the unsigned transaction when commit gets a filename.
     */
    fun createUnsignedTransaction(txData: TxData, filename: String): Boolean =
        withSession { wallet ->
            atRestOnline(wallet) {
                wallet.disposePendingTransaction()
                txData.createPocketChange(wallet)
                val pending = wallet.createTransaction(txData)
                if (pending.status !== PendingTransaction.Status.Status_Ok) {
                    val error = pending.getErrorString()
                    wallet.disposePendingTransaction()
                    throw IllegalStateException("Create unsigned transaction failed: " + error)
                }
                val saved = pending.commit(filename, true)
                val error = if (saved) null else pending.getErrorString()
                wallet.disposePendingTransaction()
                if (!saved) throw IllegalStateException("Saving unsigned transaction failed: " + error)
                Timber.i("Keystone unsigned transaction saved")
                true
            }
        } ?: throw IllegalStateException("Create unsigned transaction failed: Wallet is NULL")

    fun submitSignedTransaction(filename: String): Boolean =
        withSession { wallet ->
            atRestOnline(wallet) {
                val submitted = wallet.submitTransaction(filename)
                if (!submitted) {
                    val reason = wallet.status.errorString
                        .takeIf { !it.isNullOrBlank() }
                        ?: "wallet2 rejected the signed transaction"
                    Timber.e("Keystone signed transaction submit failed: %s", reason)
                    throw IllegalStateException("Signed transaction submit failed: " + reason)
                }
                Timber.i("Keystone signed transaction submitted")
                listener?.updated = true
                wallet.refreshHistory()
                true
            }
        } ?: throw IllegalStateException("Wallet is NULL")

''')

kit = "monero-kit-android/monerokit/src/main/java/io/horizontalsystems/monerokit/MoneroKit.kt"
insert_before(
    kit,
    "    /**\n     * Account 0's subaddresses, with list position equal to addressIndex, and what each one received.\n",
r'''    fun exportOutputs(filename: String, all: Boolean = true): Boolean =
        walletService.exportOutputs(filename, all)

    fun importKeyImages(filename: String): Boolean =
        walletService.importKeyImages(filename)

    fun createUnsignedTransaction(
        amount: Long,
        address: String,
        filename: String,
        sweepAll: Boolean = false
    ): Boolean {
        val txData = buildTxData(amount, address, null, sweepAll)
        return walletService.createUnsignedTransaction(txData, filename)
    }

    fun submitSignedTransaction(filename: String): Boolean =
        walletService.submitSignedTransaction(filename)

''')

# ---------------------------------------------------------------------------
# Persist the watch-only half in encrypted prefs. Spend key / seed never exist
# on the phone.
# ---------------------------------------------------------------------------
secrets = "app/src/main/java/one/monero/moneroone/core/wallet/WalletSecrets.kt"
rep(
    secrets,
    '    private fun pinHashKey(id: String) = "wallet.$id.pin_hash"\n',
    '    private fun pinHashKey(id: String) = "wallet.$id.pin_hash"\n'
    '    private fun watchAddressKey(id: String) = "wallet.$id.watch_address"\n'
    '    private fun watchViewKey(id: String) = "wallet.$id.watch_view_key"\n'
)
insert_before(
    secrets,
    "    fun savePinHash(id: String, hash: String) {\n",
r'''    fun saveWatchOnly(id: String, address: String, privateViewKey: String) {
        prefs.edit()
            .putString(watchAddressKey(id), address)
            .putString(watchViewKey(id), privateViewKey)
            .apply()
    }

    fun loadWatchOnly(id: String): Pair<String, String>? {
        val address = prefs.getString(watchAddressKey(id), null) ?: return null
        val viewKey = prefs.getString(watchViewKey(id), null) ?: return null
        return address to viewKey
    }

''')
rep(
    secrets,
    "            .remove(seedTypeKey(id))\n            .remove(pinHashKey(id))\n",
    "            .remove(seedTypeKey(id))\n"
    "            .remove(watchAddressKey(id))\n"
    "            .remove(watchViewKey(id))\n"
    "            .remove(pinHashKey(id))\n"
)

cache_ids = "app/src/main/java/one/monero/moneroone/core/wallet/WalletCacheIds.kt"
insert_before(
    cache_ids,
    "    // --- Duplicate-seed detection --------------------------------------------\n",
r'''    fun watchOnlyWalletId(address: String, syncResetCount: Int): String {
        val resetSuffix = if (syncResetCount > 0) syncResetCount.toString() else ""
        return stableWalletId("watch-only:" + address + resetSuffix)
    }

''')

info = "app/src/main/java/one/monero/moneroone/core/wallet/WalletInfo.kt"
rep(
    info,
    "    val isViewOnly: Boolean\n        get() = source.isViewOnly\n",
    "    val isViewOnly: Boolean\n"
    "        get() = source.isViewOnly\n\n"
    "    val isKeystone: Boolean\n"
    "        get() = source == WalletSource.VIEW_ONLY && deviceWalletId == \"keystone\"\n"
)

# ---------------------------------------------------------------------------
# ViewModel: open Keystone rows as Seed.WatchOnly and add hardware-specific
# transaction file operations.
# ---------------------------------------------------------------------------
vm = "app/src/main/java/one/monero/moneroone/core/wallet/WalletViewModel.kt"
data = read(vm)
start = data.index("        val seedData = secrets.loadSeed(active.id) ?: run {")
end = data.index("\n\n        var info = active", start)
new_seed = r'''        val watchOnly = if (active.isKeystone) secrets.loadWatchOnly(active.id) else null
        val seedData = if (active.isKeystone) null else secrets.loadSeed(active.id)
        if (active.isKeystone && watchOnly == null) {
            Timber.w("openActiveWallet: missing Keystone watch-only keys")
            _walletState.update { it.copy(error = "Could not read Keystone watch-only keys") }
            return
        }
        if (!active.isKeystone && seedData == null) {
            Timber.w("openActiveWallet: no stored seed")
            return
        }'''
data = data[:start] + new_seed + data[end:]

old = "                    derivedWalletId = it.derivedWalletId\n                        ?: WalletCacheIds.derivedWalletId(seedData.first, it.syncResetCount)\n"
new = r'''                    derivedWalletId = it.derivedWalletId ?: if (it.isKeystone) {
                        WalletCacheIds.watchOnlyWalletId(checkNotNull(watchOnly).first, it.syncResetCount)
                    } else {
                        WalletCacheIds.derivedWalletId(checkNotNull(seedData).first, it.syncResetCount)
                    }
'''
if old not in data:
    raise SystemExit("openActiveWallet cache-id anchor missing")
data = data.replace(old, new, 1)

old = "            val (seedWords, seedType) = seedData\n            val nodeUri = getSelectedNode()\n"
new = r'''            val kitSeed = if (info.isKeystone) {
                val pair = checkNotNull(watchOnly)
                Seed.WatchOnly(pair.first, pair.second)
            } else {
                val pair = checkNotNull(seedData)
                moneroSeed(pair.first, pair.second)
            }
            val nodeUri = getSelectedNode()
'''
if old not in data:
    raise SystemExit("openActiveWallet seed destructure anchor missing")
data = data.replace(old, new, 1)
data = re.sub(
    r'            Timber\.d\("openActiveWallet: wallet=.*?\n',
    '            Timber.d("openActiveWallet: opening source=" + if (info.isKeystone) "keystone" else "seed")\n',
    data,
    count=1
)
old = "                seed = moneroSeed(seedWords, seedType),\n"
if old not in data:
    raise SystemExit("openActiveWallet initialize seed anchor missing")
data = data.replace(old, "                seed = kitSeed,\n", 1)

old = "            if (!healed && startState is SyncState.NotSynced && isUnloadableCacheError(startState.error)) {\n"
new = "            if (!info.isKeystone && !healed && startState is SyncState.NotSynced && isUnloadableCacheError(startState.error)) {\n"
if old not in data:
    raise SystemExit("heal condition anchor missing")
data = data.replace(old, new, 1)
data = data.replace(
    "                retainFilesForRebuild(info, seedData.first)\n",
    "                retainFilesForRebuild(info, checkNotNull(seedData).first)\n",
    1
)
write(vm, data)

keystone_add = r'''
    suspend fun addKeystoneWallet(
        pairing: KeystonePairing,
        flowId: String,
        name: String? = null
    ): Boolean = addForScreen(flowId) {
        walletMutationMutex.withLock {
            val previousActive = _activeWallet.value
            var persisted: WalletInfo? = null
            try {
                // Validate the address shape up front, then let wallet2's real
                // create/open path be authoritative for this address + view-key pair.
                withContext(Dispatchers.Default) {
                    MoneroKit.validateAddress(pairing.primaryAddress)
                }

                val derived = WalletCacheIds.watchOnlyWalletId(pairing.primaryAddress, 0)
                _wallets.value.firstOrNull {
                    it.derivedWalletId == derived || it.cachedPrimaryAddress == pairing.primaryAddress
                }?.let { throw DuplicateWalletException(it.name) }

                snapshotActiveWalletCache()

                // A previous failed/debug pairing can leave orphaned native cache files
                // under this deterministic id. Delete them so stale .keys data can never
                // override the freshly scanned Keystone pairing payload.
                withContext(Dispatchers.IO) {
                    MoneroKit.deleteWallet(context, derived)
                }

                val info = WalletInfo(
                    id = UUID.randomUUID().toString(),
                    name = name?.trim().takeUnless { it.isNullOrEmpty() } ?: "Keystone",
                    emoji = "🔐",
                    source = WalletSource.VIEW_ONLY,
                    createdAt = System.currentTimeMillis(),
                    restoreHeight = pairing.restoreHeight,
                    restoreDateMillis = 0L,
                    cachedPrimaryAddress = pairing.primaryAddress,
                    derivedWalletId = derived,
                    deviceWalletId = "keystone"
                )

                secrets.saveWatchOnly(info.id, pairing.primaryAddress, pairing.privateViewKey)
                existingPinHash()?.let { secrets.savePinHash(info.id, it) }
                store.addWallet(info)
                store.setActiveWalletId(info.id)
                _wallets.value = store.wallets()
                _activeWallet.value = info
                _walletSessionId.value += 1
                persisted = info

                cancelKitObservers()
                val kit = WalletManager.initialize(
                    context = context,
                    seed = Seed.WatchOnly(pairing.primaryAddress, pairing.privateViewKey),
                    restoreDateOrHeight = pairing.restoreHeight.toString(),
                    walletId = derived,
                    node = nodeCredentials.kitNodeString(getSelectedNode()),
                    trustNode = false,
                    networkType = NetworkType.NetworkType_Mainnet
                )
                setupKitObservers(kit)
                refreshHasWallet()
                _walletState.update {
                    it.copy(
                        isInitializing = false,
                        balance = Balance(0, 0),
                        transactions = emptyList(),
                        addresses = null,
                        error = null
                    )
                }
                publishAddresses(info, kit)
                WalletManager.start()

                // startInternal captures local wallet identity immediately after opening
                // the wallet file, before daemon setup. This remains available even when
                // the selected node is temporarily unreachable.
                val openedAddress = kit.checkedWalletFilePrimaryAddress
                val openedViewKey = kit.checkedWalletFilePrivateViewKey
                check(openedAddress == pairing.primaryAddress) {
                    "Keystone pairing failed: wallet2 opened a different primary address. Scan the normal Monero/Feather connection QR again."
                }
                check(
                    openedViewKey != null &&
                        openedViewKey.equals(pairing.privateViewKey, ignoreCase = true)
                ) {
                    "Keystone pairing failed: wallet2 opened a different private view key. Scan the normal Monero/Feather connection QR again."
                }

                val startState = kit.syncStateFlow.value
                if (startState is SyncState.NotSynced && isWalletLevelStartError(startState.error)) {
                    throw WalletOpenException(startState.error.message ?: "Keystone wallet could not be opened")
                }
                publishAddresses(info, kit)
                true
            } catch (e: DuplicateWalletException) {
                _walletState.update { it.copy(isInitializing = false, error = e.message) }
                false
            } catch (e: Exception) {
                Timber.e(e, "Failed to add Keystone wallet")
                persisted?.let { rollbackFailedAdd(it, previousActive) }
                _walletState.update {
                    it.copy(isInitializing = false, error = e.message ?: "Failed to add Keystone wallet")
                }
                false
            }
        }
    }

'''
insert_before(vm, "    private suspend fun addWalletInternal(\n", keystone_add)

keystone_ops = r'''    private fun requireActiveKeystone(): Pair<WalletInfo, MoneroKit> {
        val info = _activeWallet.value ?: error("No active wallet")
        check(info.isKeystone) { "Active wallet is not hardware-backed." }
        val kit = WalletManager.kit ?: error("Wallet is not connected yet")
        check(WalletManager.currentWalletId == info.derivedWalletId) { "Active wallet changed — please retry" }
        return info to kit
    }

    suspend fun keystoneExportOutputs(): ByteArray = withContext(Dispatchers.IO) {
        val (info, kit) = requireActiveKeystone()
        val file = File.createTempFile("keystone_outputs_", ".bin", context.cacheDir)
        try {
            check(kit.exportOutputs(file.absolutePath, true)) { "Could not export wallet outputs" }
            check(_activeWallet.value?.id == info.id) { "Active wallet changed — please retry" }
            file.readBytes()
        } finally {
            file.delete()
        }
    }

    suspend fun keystoneImportKeyImages(data: ByteArray) = withContext(Dispatchers.IO) {
        require(data.isNotEmpty()) { "Key-image QR was empty" }
        val (info, kit) = requireActiveKeystone()
        Timber.i("Keystone key-image QR decoded: %d bytes", data.size)
        val file = File.createTempFile("keystone_keyimages_", ".bin", context.cacheDir)
        try {
            file.writeBytes(data)
            check(kit.importKeyImages(file.absolutePath)) { "Key image sync failed" }
            check(_activeWallet.value?.id == info.id) { "Active wallet changed — please retry" }
            Timber.i("Keystone key-image sync complete")
        } finally {
            file.delete()
        }
    }

    suspend fun keystoneCreateUnsignedTransaction(
        address: String,
        amount: Long,
        sweepAll: Boolean
    ): ByteArray = withContext(Dispatchers.IO) {
        MoneroKit.validateAddress(address)
        check(sweepAll || amount > 0L) { "Invalid amount" }
        val (info, kit) = requireActiveKeystone()
        val file = File.createTempFile("keystone_unsigned_", ".bin", context.cacheDir)
        try {
            check(kit.createUnsignedTransaction(amount, address, file.absolutePath, sweepAll)) {
                "Could not create unsigned transaction"
            }
            check(_activeWallet.value?.id == info.id) { "Active wallet changed — please retry" }
            file.readBytes()
        } finally {
            file.delete()
        }
    }

    suspend fun keystoneSubmitSignedTransaction(data: ByteArray): Boolean = withContext(Dispatchers.IO) {
        require(data.isNotEmpty()) { "Signed transaction QR was empty" }
        val (info, kit) = requireActiveKeystone()
        val file = File.createTempFile("keystone_signed_", ".bin", context.cacheDir)
        try {
            file.writeBytes(data)
            Timber.i("Keystone signed-tx QR decoded: %d bytes", data.size)
            val submitted = kit.submitSignedTransaction(file.absolutePath)
            check(_activeWallet.value?.id == info.id) { "Active wallet changed — please retry" }
            submitted
        } finally {
            file.delete()
        }
    }

'''
insert_before(vm, "    fun send(flow: SendFlow, address: String, amount: Long, memo: String? = null, isSweepAll: Boolean = false) {\n", keystone_ops)

# ---------------------------------------------------------------------------
# Pair entry points + hardware send handoff.
# ---------------------------------------------------------------------------
welcome = "app/src/main/java/one/monero/moneroone/ui/screens/onboarding/WelcomeScreen.kt"
rep(
    welcome,
    "import androidx.compose.material.icons.filled.ReplayCircleFilled\n",
    "import androidx.compose.material.icons.filled.ReplayCircleFilled\nimport androidx.compose.material.icons.filled.QrCodeScanner\n"
)
rep(
    welcome,
    "fun WelcomeScreen(\n    onCreateWallet: () -> Unit,\n    onRestoreWallet: () -> Unit\n) {\n",
    "fun WelcomeScreen(\n    onCreateWallet: () -> Unit,\n    onRestoreWallet: () -> Unit,\n    onPairKeystone: () -> Unit = {}\n) {\n"
)
insert_before(
    welcome,
    "            Spacer(modifier = Modifier.height(32.dp))\n        }\n",
r'''            Spacer(modifier = Modifier.height(12.dp))

            PrimaryButton(
                onClick = onPairKeystone,
                modifier = Modifier.fillMaxWidth(),
                contentColor = MaterialTheme.colorScheme.onSurface
            ) {
                Icon(Icons.Filled.QrCodeScanner, contentDescription = null, modifier = Modifier.size(20.dp))
                Text(text = "Pair Keystone")
            }

''')

add_screen = "app/src/main/java/one/monero/moneroone/ui/screens/wallet/AddWalletScreen.kt"
rep(
    add_screen,
    "import androidx.compose.material.icons.filled.ReplayCircleFilled\n",
    "import androidx.compose.material.icons.filled.ReplayCircleFilled\nimport androidx.compose.material.icons.filled.QrCodeScanner\n"
)
rep(
    add_screen,
    "    onCreateWallet: () -> Unit,\n    onRestoreWallet: () -> Unit,\n    onBack: () -> Unit\n",
    "    onCreateWallet: () -> Unit,\n    onRestoreWallet: () -> Unit,\n    onPairKeystone: () -> Unit,\n    onBack: () -> Unit\n"
)
insert_before(
    add_screen,
    "            Spacer(modifier = Modifier.height(32.dp))\n        }\n",
r'''            Spacer(modifier = Modifier.height(12.dp))

            PrimaryButton(
                onClick = onPairKeystone,
                modifier = Modifier.fillMaxWidth()
            ) {
                Icon(Icons.Filled.QrCodeScanner, contentDescription = null, modifier = Modifier.size(20.dp))
                Text("Pair Keystone")
            }

''')

send_screen = "app/src/main/java/one/monero/moneroone/ui/screens/send/SendScreen.kt"
rep(
    send_screen,
    "    onScanQr: () -> Unit,\n    onSent: () -> Unit\n) {\n",
    "    onScanQr: () -> Unit,\n    onSent: () -> Unit,\n    onKeystoneSign: (String, Long, Boolean) -> Unit = { _, _, _ -> }\n) {\n"
)
rep(
    send_screen,
r'''            onAuthenticated = {
                showAuthGate = false
                walletViewModel.send(
                    flow,
                    address,
                    walletViewModel.parseXmr(amount),
                    isSweepAll = isSweepAll
                )
            },
''',
r'''            onAuthenticated = {
                showAuthGate = false
                val atomic = walletViewModel.parseXmr(amount)
                if (walletViewModel.activeWallet.value?.isKeystone == true) {
                    onKeystoneSign(address, atomic, isSweepAll)
                } else {
                    walletViewModel.send(
                        flow,
                        address,
                        atomic,
                        isSweepAll = isSweepAll
                    )
                }
            },
''')

nav = "app/src/main/java/one/monero/moneroone/ui/navigation/NavGraph.kt"
rep(
    nav,
    "import one.monero.moneroone.ui.screens.wallet.AddWalletScreen\n",
    "import one.monero.moneroone.ui.screens.wallet.AddWalletScreen\n"
    "import one.monero.moneroone.ui.screens.keystone.KeystonePairScreen\n"
    "import one.monero.moneroone.ui.screens.keystone.KeystoneSignScreen\n"
    "import one.monero.moneroone.ui.screens.keystone.KeystoneKeyImageSyncScreen\n"
)
rep(
    nav,
    '    data object AddWallet : Screen("add_wallet")\n',
r'''    data object AddWallet : Screen("add_wallet")
    data object KeystonePair : Screen("keystone_pair?adding={adding}") {
        fun createRoute(adding: Boolean = false) = "keystone_pair?adding=" + adding
    }
    data object KeystoneSign : Screen("keystone_sign?address={address}&amount={amount}&sweep={sweep}") {
        fun createRoute(address: String, amount: Long, sweep: Boolean): String =
            "keystone_sign?address=" + android.net.Uri.encode(address) + "&amount=" + amount + "&sweep=" + sweep
    }
    data object KeystoneKeyImages : Screen("keystone_key_images")
''')
rep(
    nav,
    "            walletViewModel.activeWallet.value?.isViewOnly == true ->\n",
    "            walletViewModel.activeWallet.value?.isViewOnly == true && walletViewModel.activeWallet.value?.isKeystone != true ->\n"
)
rep(
    nav,
    "                    onRestoreWallet = { navController.navigate(Screen.RestoreWallet.createRoute(adding = false)) }\n",
    "                    onRestoreWallet = { navController.navigate(Screen.RestoreWallet.createRoute(adding = false)) },\n"
    "                    onPairKeystone = { navController.navigate(Screen.KeystonePair.createRoute(adding = false)) }\n"
)
rep(
    nav,
    "                    onRestoreWallet = { navController.navigate(Screen.RestoreWallet.createRoute(adding = true)) },\n                    onBack = { navController.popBackStack() }\n",
    "                    onRestoreWallet = { navController.navigate(Screen.RestoreWallet.createRoute(adding = true)) },\n"
    "                    onPairKeystone = { navController.navigate(Screen.KeystonePair.createRoute(adding = true)) },\n"
    "                    onBack = { navController.popBackStack() }\n"
)

pair_route = r'''
            composable(
                route = Screen.KeystonePair.route,
                arguments = listOf(
                    navArgument("adding") {
                        type = NavType.BoolType
                        defaultValue = false
                    }
                )
            ) { backStackEntry ->
                val adding = backStackEntry.arguments?.getBoolean("adding") ?: false
                KeystonePairScreen(
                    walletViewModel = walletViewModel,
                    flowId = backStackEntry.id,
                    isAddingWallet = adding,
                    onPaired = {
                        if (adding) {
                            navController.popBackStack(Screen.Main.route, inclusive = false)
                        } else {
                            navController.navigate(Screen.SetPin.route) {
                                popUpTo(Screen.Welcome.route) { inclusive = true }
                            }
                        }
                    },
                    onBack = { navController.popBackStack() }
                )
            }

'''
insert_before(nav, "            composable(Screen.SetPin.route) {\n", pair_route)

rep(
    nav,
    "                    onSent = {\n                        navController.navigate(Screen.Main.route) {\n                            popUpTo(Screen.Main.route) { inclusive = true }\n                        }\n                    }\n",
    "                    onSent = {\n                        navController.navigate(Screen.Main.route) {\n                            popUpTo(Screen.Main.route) { inclusive = true }\n                        }\n                    },\n"
    "                    onKeystoneSign = { dest, atomic, sweep ->\n"
    "                        navController.navigate(Screen.KeystoneSign.createRoute(dest, atomic, sweep))\n"
    "                    }\n"
)

sign_route = r'''
            composable(
                route = Screen.KeystoneSign.route,
                arguments = listOf(
                    navArgument("address") { type = NavType.StringType },
                    navArgument("amount") { type = NavType.LongType; defaultValue = 0L },
                    navArgument("sweep") { type = NavType.BoolType; defaultValue = false }
                )
            ) { backStackEntry ->
                KeystoneSignScreen(
                    walletViewModel = walletViewModel,
                    address = backStackEntry.arguments?.getString("address").orEmpty(),
                    amountAtomic = backStackEntry.arguments?.getLong("amount") ?: 0L,
                    sweepAll = backStackEntry.arguments?.getBoolean("sweep") ?: false,
                    onBack = { navController.popBackStack() },
                    onSent = {
                        navController.navigate(Screen.Main.route) {
                            popUpTo(Screen.Main.route) { inclusive = true }
                        }
                    }
                )
            }

'''
insert_before(nav, "            composable(Screen.QRScanner.route) {\n", sign_route)


# ---------------------------------------------------------------------------
# Keystone balance / scan maintenance.
# ---------------------------------------------------------------------------

# wallet2's public API already supports rescanSpent(), but monero-kit never
# exposed it through JNI. Add the minimal bridge.
rep(
    wallet_java,
    "//virtual bool rescanSpent() = 0;\n",
    "public native boolean rescanSpent();\n"
)
rep(
    cpp,
    "//virtual bool rescanSpent() = 0;\n",
r'''JNIEXPORT jboolean JNICALL
Java_io_horizontalsystems_monerokit_model_Wallet_rescanSpent(
        JNIEnv *env, jobject instance) {
    Monero::Wallet *wallet = getHandle<Monero::Wallet>(env, instance);
    bool success = false;
    try {
        success = wallet->rescanSpent();
    } catch (const std::exception &e) {
        throwIllegalState(env, std::string("Rescan spent failed: ") + e.what());
    } catch (...) {
        throwIllegalState(env, "Rescan spent failed");
    }
    return static_cast<jboolean>(success);
}

''')

# Add a reusable trusted-daemon spent rescan and run it best-effort after a
# signed Keystone transaction is successfully broadcast. A rescan failure must
# never turn an already-broadcast transaction into a reported send failure.
_service_data = read(service)
_service_anchor = r'''    /**
     * A watch-only wallet creates the transaction set but saves it instead of
'''
_service_rescan = r'''    fun rescanSpent(): Boolean =
        withSession { wallet ->
            atRestOnline(wallet) {
                val wasTrusted = wallet.trustedDaemon()
                try {
                    wallet.setTrustedDaemon(true)
                    val rescanned = wallet.rescanSpent()
                    if (!rescanned) {
                        val reason = wallet.status.errorString
                            .takeIf { !it.isNullOrBlank() }
                            ?: "wallet2 could not rescan spent outputs"
                        Timber.e("Rescan spent failed: %s", reason)
                        throw IllegalStateException("Rescan spent failed: " + reason)
                    }
                    Timber.i("Rescan spent succeeded")
                    listener?.updated = true
                    wallet.refreshHistory()
                    storeCache(wallet)
                    true
                } finally {
                    wallet.setTrustedDaemon(wasTrusted)
                }
            }
        } ?: throw IllegalStateException("Wallet is NULL")

'''
if _service_anchor not in _service_data:
    raise SystemExit("WalletService rescan insert anchor missing")
_service_data = _service_data.replace(_service_anchor, _service_rescan + _service_anchor, 1)

_submit_old = r'''                Timber.i("Keystone signed transaction submitted")
                listener?.updated = true
                wallet.refreshHistory()
                true
'''
_submit_new = r'''                Timber.i("Keystone signed transaction submitted")

                val wasTrusted = wallet.trustedDaemon()
                try {
                    wallet.setTrustedDaemon(true)
                    if (!wallet.rescanSpent()) {
                        Timber.w("Post-send rescan spent failed: %s", wallet.status.errorString)
                    } else {
                        Timber.i("Post-send rescan spent succeeded")
                    }
                } catch (t: Throwable) {
                    Timber.w(t, "Post-send rescan spent failed after broadcast")
                } finally {
                    wallet.setTrustedDaemon(wasTrusted)
                }

                listener?.updated = true
                wallet.refreshHistory()
                storeCache(wallet)
                true
'''
if _submit_old not in _service_data:
    raise SystemExit("WalletService signed-submit balance anchor missing")
_service_data = _service_data.replace(_submit_old, _submit_new, 1)
write(service, _service_data)

_kit_data = read(kit)
_kit_anchor = r'''    fun importKeyImages(filename: String): Boolean =
        walletService.importKeyImages(filename)

'''
_kit_new = r'''    fun importKeyImages(filename: String): Boolean =
        walletService.importKeyImages(filename)

    fun rescanSpent(): Boolean =
        walletService.rescanSpent()

'''
if _kit_anchor not in _kit_data:
    raise SystemExit("MoneroKit rescan anchor missing")
write(kit, _kit_data.replace(_kit_anchor, _kit_new, 1))

# The normal reset-sync path requires a seed. Keystone intentionally has no
# spend seed on the phone, so rebuild it from the encrypted watch-only address
# and private view key, keeping old cache files for recovery.
_vm_data = read(vm)
_reset_old = r'''            val active = _activeWallet.value ?: return
            val seedData = secrets.loadSeed(active.id) ?: run {
                Timber.w("No stored seed, cannot reset sync")
                _walletState.update { it.copy(error = "No wallet seed to reset sync") }
                return
            }

            // Never replace a wallet whose stored seed has not been verified against its file.
            check(verifySeedForExport(active.id)) {
                "Cannot reset sync until the seed matches the wallet file. Original files preserved."
            }
            cancelKitObservers()
            WalletManager.stopAndRelease()
            retainFilesForRebuild(active, seedData.first)
'''
_reset_new = r'''            val active = _activeWallet.value ?: return

            if (active.isKeystone) {
                val watchOnly = secrets.loadWatchOnly(active.id) ?: run {
                    Timber.w("No stored Keystone watch-only keys, cannot reset sync")
                    _walletState.update { it.copy(error = "Missing Keystone watch-only keys") }
                    return
                }

                cancelKitObservers()
                WalletManager.stopAndRelease()

                val nextReset = active.syncResetCount + 1
                val nextCacheId = WalletCacheIds.watchOnlyWalletId(watchOnly.first, nextReset)
                checkNotNull(mergeWalletUpdate(active.id) {
                    it.copy(
                        syncResetCount = nextReset,
                        derivedWalletId = nextCacheId,
                        retainedCacheIds = (it.retainedCacheIds + listOfNotNull(it.derivedWalletId)).distinct(),
                        cachedBalance = 0L,
                        cachedUnlockedBalance = 0L
                    )
                }) { "Wallet was removed during recovery" }
            } else {
                val seedData = secrets.loadSeed(active.id) ?: run {
                    Timber.w("No stored seed, cannot reset sync")
                    _walletState.update { it.copy(error = "No wallet seed to reset sync") }
                    return
                }

                // Never replace a wallet whose stored seed has not been verified against its file.
                check(verifySeedForExport(active.id)) {
                    "Cannot reset sync until the seed matches the wallet file. Original files preserved."
                }
                cancelKitObservers()
                WalletManager.stopAndRelease()
                retainFilesForRebuild(active, seedData.first)
            }
'''
if _reset_old not in _vm_data:
    raise SystemExit("Keystone reset-sync anchor missing")
_vm_data = _vm_data.replace(_reset_old, _reset_new, 1)

_vm_ki_anchor = r'''    suspend fun keystoneImportKeyImages(data: ByteArray) = withContext(Dispatchers.IO) {
'''
_vm_ki_method = r'''    suspend fun keystoneRescanSpent(): Boolean = withContext(Dispatchers.IO) {
        val (_, kit) = requireActiveKeystone()
        check(kit.rescanSpent()) { "Rescan spent failed" }
        true
    }

'''
if _vm_ki_anchor not in _vm_data:
    raise SystemExit("Keystone rescan ViewModel anchor missing")
_vm_data = _vm_data.replace(_vm_ki_anchor, _vm_ki_method + _vm_ki_anchor, 1)
write(vm, _vm_data)

# Sync Settings: allow a literal block height, Feather-style spent rescan, and
# direct navigation into standalone Keystone key-image sync.
sync_settings = "app/src/main/java/one/monero/moneroone/ui/screens/settings/SyncSettingsScreen.kt"
_sync = read(sync_settings)
_sync = _sync.replace(
    "import androidx.compose.material3.LinearProgressIndicator\n",
    "import androidx.compose.material3.LinearProgressIndicator\nimport androidx.compose.material3.CircularProgressIndicator\nimport androidx.compose.material3.OutlinedTextField\n"
)
_sync = _sync.replace(
    "import androidx.compose.runtime.remember\n",
    "import androidx.compose.runtime.remember\nimport androidx.compose.runtime.rememberCoroutineScope\n"
)
_sync = _sync.replace(
    "import java.util.Locale\n",
    "import java.util.Locale\nimport kotlinx.coroutines.launch\n"
)
_sync = _sync.replace(
r'''fun SyncSettingsScreen(
    walletViewModel: WalletViewModel,
    onBack: () -> Unit,
    onNodeSettingsClick: () -> Unit
) {
''',
r'''fun SyncSettingsScreen(
    walletViewModel: WalletViewModel,
    onBack: () -> Unit,
    onNodeSettingsClick: () -> Unit,
    onKeystoneKeyImageSync: () -> Unit = {}
) {
''',
    1
)
_sync = _sync.replace(
r'''    var showDatePicker by remember { mutableStateOf(false) }
    // Restore height/date are per-wallet, from the active WalletInfo.
''',
r'''    val scope = rememberCoroutineScope()
    var showDatePicker by remember { mutableStateOf(false) }
    var showHeightDialog by remember { mutableStateOf(false) }
    var heightText by remember { mutableStateOf("") }
    var showRescanSpentConfirm by remember { mutableStateOf(false) }
    var spentRescanRunning by remember { mutableStateOf(false) }
    var maintenanceMessage by remember { mutableStateOf<String?>(null) }
    // Restore height/date are per-wallet, from the active WalletInfo.
''',
    1
)

_scan_card_end = r'''        // Background Sync section: last, where iOS has its background
'''
_scan_maintenance = r'''        TextButton(
            onClick = {
                heightText = restoreHeight.toString()
                showHeightDialog = true
            },
            modifier = Modifier.align(Alignment.End)
        ) {
            Text(tr("Enter block height"), color = MoneroOrange)
        }

        if (activeWallet?.isKeystone == true) {
            SettingsSectionHeader(tr("Hardware Wallet Maintenance"))

            GlassCard(
                modifier = Modifier.fillMaxWidth(),
                onClick = onKeystoneKeyImageSync,
                cornerRadius = 16.dp,
                shadow = false
            ) {
                Row(
                    modifier = Modifier.fillMaxWidth().padding(16.dp),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Icon(
                        imageVector = Icons.Default.Sync,
                        contentDescription = null,
                        tint = MoneroOrange,
                        modifier = Modifier.size(24.dp)
                    )
                    Spacer(modifier = Modifier.width(16.dp))
                    Column(modifier = Modifier.weight(1f)) {
                        Text(
                            text = tr("Sync Key Images with Keystone"),
                            style = MaterialTheme.typography.titleSmall,
                            fontWeight = FontWeight.Medium
                        )
                        Text(
                            text = tr("Update spent/unspent status from the hardware wallet without sending."),
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                    Icon(
                        imageVector = Icons.AutoMirrored.Filled.KeyboardArrowRight,
                        contentDescription = null,
                        tint = MoneroTheme.colors.labelTertiary,
                        modifier = Modifier.size(20.dp)
                    )
                }
            }

            Spacer(modifier = Modifier.height(10.dp))

            GlassCard(
                modifier = Modifier.fillMaxWidth(),
                onClick = {
                    if (!spentRescanRunning) showRescanSpentConfirm = true
                },
                cornerRadius = 16.dp,
                shadow = false
            ) {
                Row(
                    modifier = Modifier.fillMaxWidth().padding(16.dp),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    if (spentRescanRunning) {
                        CircularProgressIndicator(modifier = Modifier.size(24.dp))
                    } else {
                        Icon(
                            imageVector = Icons.Default.Sync,
                            contentDescription = null,
                            tint = MoneroOrange,
                            modifier = Modifier.size(24.dp)
                        )
                    }
                    Spacer(modifier = Modifier.width(16.dp))
                    Column(modifier = Modifier.weight(1f)) {
                        Text(
                            text = tr("Rescan Spent Outputs"),
                            style = MaterialTheme.typography.titleSmall,
                            fontWeight = FontWeight.Medium
                        )
                        Text(
                            text = tr("Recheck known key images against the selected node."),
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                }
            }

            maintenanceMessage?.let { message ->
                Spacer(modifier = Modifier.height(8.dp))
                Text(
                    text = message,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
        }

'''
if _scan_card_end not in _sync:
    raise SystemExit("Sync Settings maintenance insert anchor missing")
_sync = _sync.replace(_scan_card_end, _scan_maintenance + _scan_card_end, 1)

# Warn Keystone users that a full cache rebuild cannot derive key images from
# the view key alone.
_sync = _sync.replace(
r'''                text = { Text(tr("Scanning restarts at block %s. Earlier transactions won't be found.", formatHeight(newHeight))) },
''',
r'''                text = {
                    Text(
                        if (activeWallet?.isKeystone == true) {
                            tr("Scanning restarts at block %s. Earlier transactions won't be found. After the rescan finishes, sync key images with Keystone so spent outputs are accurate.", formatHeight(newHeight))
                        } else {
                            tr("Scanning restarts at block %s. Earlier transactions won't be found.", formatHeight(newHeight))
                        }
                    )
                },
''',
    1
)

_sync_dialog_anchor = "\n}\n\nprivate fun getSyncStatusText(syncState: SyncState): String {\n"
_sync_dialogs = r'''
    if (showHeightDialog) {
        AlertDialog(
            onDismissRequest = { showHeightDialog = false },
            title = { Text(tr("Set Restore Height")) },
            text = {
                Column {
                    Text(tr("Enter the block height to start scanning from. Use an earlier height than the wallet's first transaction."))
                    Spacer(modifier = Modifier.height(12.dp))
                    OutlinedTextField(
                        value = heightText,
                        onValueChange = { value -> heightText = value.filter { it.isDigit() }.take(10) },
                        label = { Text(tr("Block height")) },
                        singleLine = true
                    )
                }
            },
            confirmButton = {
                TextButton(
                    onClick = {
                        val newHeight = heightText.toLongOrNull()
                        if (newHeight != null && newHeight >= 0L) {
                            showHeightDialog = false
                            walletViewModel.setRestoreHeight(newHeight, 0L)
                            walletViewModel.resetSync()
                            maintenanceMessage = if (activeWallet?.isKeystone == true) {
                                tr("Blockchain rescan started. Sync key images with Keystone again after it finishes.")
                            } else {
                                tr("Blockchain rescan started.")
                            }
                        }
                    },
                    enabled = heightText.toLongOrNull() != null
                ) {
                    Text(tr("Rescan"), color = MoneroOrange)
                }
            },
            dismissButton = {
                DismissTextButton(onClick = { showHeightDialog = false }) {
                    Text(tr("Cancel"))
                }
            }
        )
    }

    if (showRescanSpentConfirm) {
        AlertDialog(
            onDismissRequest = { showRescanSpentConfirm = false },
            title = { Text(tr("Rescan Spent Outputs?")) },
            text = {
                Text(
                    tr("This asks the selected Monero node which of your known key images are spent. That reveals which queried outputs belong to this wallet, so use your own or a node you trust.")
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    showRescanSpentConfirm = false
                    spentRescanRunning = true
                    maintenanceMessage = null
                    scope.launch {
                        try {
                            walletViewModel.keystoneRescanSpent()
                            maintenanceMessage = tr("Spent outputs rescanned. Balance refreshed.")
                        } catch (t: Throwable) {
                            maintenanceMessage = t.message ?: tr("Rescan spent failed")
                        } finally {
                            spentRescanRunning = false
                        }
                    }
                }) {
                    Text(tr("Rescan"), color = MoneroOrange)
                }
            },
            dismissButton = {
                DismissTextButton(onClick = { showRescanSpentConfirm = false }) {
                    Text(tr("Cancel"))
                }
            }
        )
    }
'''
if _sync_dialog_anchor not in _sync:
    raise SystemExit("Sync Settings dialog insert anchor missing")
_sync = _sync.replace(_sync_dialog_anchor, _sync_dialogs + _sync_dialog_anchor, 1)
write(sync_settings, _sync)



rep(
    nav,
r'''                    onBack = { navController.popBackStack() },
                    onNodeSettingsClick = { navController.navigate(Screen.NodeSettings.route) }
''',
r'''                    onBack = { navController.popBackStack() },
                    onNodeSettingsClick = { navController.navigate(Screen.NodeSettings.route) },
                    onKeystoneKeyImageSync = { navController.navigate(Screen.KeystoneKeyImages.route) }
''')
keystone_key_images_route = r'''
            composable(Screen.KeystoneKeyImages.route) {
                KeystoneKeyImageSyncScreen(
                    walletViewModel = walletViewModel,
                    onBack = { navController.popBackStack() }
                )
            }

'''
insert_before(nav, "            composable(Screen.NodeSettings.route) {\n", keystone_key_images_route)

# ---------------------------------------------------------------------------
# Copy new source files into Monero One.
# ---------------------------------------------------------------------------
copy_asset(
    "core/KeystoneSupport.kt",
    "app/src/main/java/one/monero/moneroone/core/wallet/KeystoneSupport.kt"
)
for name in ["KeystoneQr.kt", "KeystonePairScreen.kt", "KeystoneSignScreen.kt", "KeystoneKeyImageSyncScreen.kt"]:
    copy_asset(
        "ui/" + name,
        "app/src/main/java/one/monero/moneroone/ui/screens/keystone/" + name
    )

print("Keystone patch applied")
