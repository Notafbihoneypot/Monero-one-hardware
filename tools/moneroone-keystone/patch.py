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
rep(
    wallet_service_fast,
r'''                    if (!wallet.isSynchronized) {
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
''',
r'''                    if (!wallet.isSynchronized) {
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
''')

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

    fun importKeyImages(filename: String): Boolean =
        withSession { wallet ->
            wallet.importKeyImages(filename).also { imported ->
                if (imported) {
                    wallet.refreshHistory()
                    listener?.updated = true
                }
            }
        } ?: throw IllegalStateException("Wallet is NULL")

    /**
     * A watch-only wallet creates the transaction set but saves it instead of
     * broadcasting. wallet2 writes the unsigned transaction when commit gets a filename.
     */
    fun createUnsignedTransaction(txData: TxData, filename: String): Boolean {
        if (wallet == null) throw IllegalStateException("Create unsigned transaction failed: Wallet is NULL")
        return sessionLock.withLock {
            val current = wallet ?: throw IllegalStateException("Create unsigned transaction failed: Wallet is NULL")
            check(refreshingWallet === current) { "Wallet is not connected" }
            current.disposePendingTransaction()
            txData.createPocketChange(current)
            val pending = current.createTransaction(txData)
            if (pending.status !== PendingTransaction.Status.Status_Ok) {
                val error = pending.getErrorString()
                current.disposePendingTransaction()
                throw IllegalStateException("Create unsigned transaction failed: " + error)
            }
            val saved = pending.commit(filename, true)
            val error = if (saved) null else pending.getErrorString()
            current.disposePendingTransaction()
            if (!saved) throw IllegalStateException("Saving unsigned transaction failed: " + error)
            true
        }
    }

    fun submitSignedTransaction(filename: String): Boolean =
        withSession { wallet ->
            check(refreshingWallet === wallet) { "Wallet is not connected" }
            wallet.submitTransaction(filename).also { submitted ->
                if (submitted) {
                    listener?.updated = true
                    wallet.refreshHistory()
                }
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
        val file = File.createTempFile("keystone_keyimages_", ".bin", context.cacheDir)
        try {
            file.writeBytes(data)
            check(kit.importKeyImages(file.absolutePath)) { "Key image sync failed" }
            check(_activeWallet.value?.id == info.id) { "Active wallet changed — please retry" }
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
# Copy new source files into Monero One.
# ---------------------------------------------------------------------------
copy_asset(
    "core/KeystoneSupport.kt",
    "app/src/main/java/one/monero/moneroone/core/wallet/KeystoneSupport.kt"
)
for name in ["KeystoneQr.kt", "KeystonePairScreen.kt", "KeystoneSignScreen.kt"]:
    copy_asset(
        "ui/" + name,
        "app/src/main/java/one/monero/moneroone/ui/screens/keystone/" + name
    )

print("Keystone patch applied")
