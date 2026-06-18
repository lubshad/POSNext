/**
 * Remote Print Store
 *
 * Manages two sides of remote printing:
 *
 * 1. Hub side — this device shares its local QZ Tray printers:
 *    - Registers printers via register_remote_printer
 *    - Sends periodic heartbeats to stay "online"
 *    - Listens for pos_remote_print_job Socket.IO events
 *    - Claims jobs, fetches payload, prints via QZ Tray, reports status
 *
 * 2. Sender side — this device sends print jobs to remote printers:
 *    - Lists available remote printers
 *    - Creates print jobs via create_remote_print_job
 */

import { defineStore } from "pinia"
import { ref, computed } from "vue"
import { call } from "@/utils/apiWrapper"
import { logger } from "@/utils/logger"
import { printWithSilentFallback } from "@/utils/printInvoice"
import { printClosingReportWithFallback } from "@/utils/printClosingReport"
import {
	qzConnected,
	connect as qzConnect,
} from "@/utils/qzTray"

const log = logger.create("RemotePrint")

const HUB_ID_KEY = "pos_remote_print_hub_id"
const HEARTBEAT_INTERVAL_MS = 30000 // 30 seconds
const PRINT_JOB_EVENT = "pos_remote_print_job"

/**
 * Get or generate a stable hub ID for this device.
 * Persisted in localStorage so it survives page reloads.
 */
function getStableHubId() {
	try {
		let id = localStorage.getItem(HUB_ID_KEY)
		if (!id) {
			id =
				typeof crypto !== "undefined" && crypto.randomUUID
					? crypto.randomUUID()
					: `hub_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`
			localStorage.setItem(HUB_ID_KEY, id)
		}
		return id
	} catch {
		return `hub_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`
	}
}

export const useRemotePrintStore = defineStore("remotePrint", () => {
	// ========================================================================
	// STATE
	// ========================================================================

	const hubId = ref(getStableHubId())

	/** Printers this device is currently sharing (hub side) */
	const sharedPrinters = ref([])

	/** Remote printers available for this device to print to (sender side) */
	const availablePrinters = ref([])

	/** Whether the hub listener is active */
	const isHubActive = ref(false)

	/** Whether a heartbeat loop is running */
	const isHeartbeatRunning = ref(false)

	/** Jobs currently being processed by this hub */
	const activeJobs = ref(new Map())

	/** Loading state for printer list fetches */
	const isLoadingPrinters = ref(false)

	let heartbeatTimer = null

	// ========================================================================
	// COMPUTED
	// ========================================================================

	const hasSharedPrinters = computed(() => sharedPrinters.value.length > 0)
	const onlinePrinters = computed(() =>
		availablePrinters.value.filter((p) => p.online),
	)

	/**
	 * Check whether a remote printer is owned by THIS device.
	 * Used to short-circuit the round-trip when printing to your own printer.
	 * @param {string} printerName - POS Remote Printer document name
	 * @returns {boolean}
	 */
	function isLocalPrinter(printerName) {
		if (!printerName) return false
		// Fast path: sharedPrinters is already filtered to this hub's printers
		if (sharedPrinters.value.some((p) => p.name === printerName)) {
			return true
		}
		// Fallback: check availablePrinters by hub_id
		const printer = availablePrinters.value.find((p) => p.name === printerName)
		return printer?.hub_id === hubId.value
	}

	// ========================================================================
	// HUB SIDE — Sharing printers & receiving jobs
	// ========================================================================

	/**
	 * Register a local QZ printer as a shared remote printer.
	 * @param {Object} params
	 * @param {string} params.printerName - Display name for other users
	 * @param {string} params.qzPrinterName - Actual system printer name
	 * @param {string[]} params.allowedTypes - Receipt | Closing Report | General
	 * @param {string[]} params.posProfiles - Optional POS Profiles to limit visibility
	 */
	async function registerPrinter({
		printerName,
		qzPrinterName,
		allowedTypes,
		posProfiles,
	}) {
		try {
			const result = await call(
				"pos_next.api.remote_print.register_remote_printer",
				{
					printer_name: printerName,
					qz_printer_name: qzPrinterName,
					hub_id: hubId.value,
					allowed_types: allowedTypes || ["General"],
					pos_profiles: posProfiles || [],
				},
			)
			log.info(`Registered remote printer: ${printerName}`)
			await refreshSharedPrinters()
			return result
		} catch (error) {
			log.error("Failed to register remote printer:", error?.message || error)
			throw error
		}
	}

	/**
	 * Unregister a shared printer (disables it).
	 * @param {string} qzPrinterName - The local printer to stop sharing
	 */
	async function unregisterPrinter(qzPrinterName) {
		try {
			await call("pos_next.api.remote_print.unregister_remote_printer", {
				hub_id: hubId.value,
				qz_printer_name: qzPrinterName,
			})
			log.info(`Unregistered remote printer: ${qzPrinterName}`)
			await refreshSharedPrinters()
		} catch (error) {
			log.error("Failed to unregister remote printer:", error?.message || error)
		}
	}

	/**
	 * Fetch the list of printers this hub is currently sharing.
	 */
	async function refreshSharedPrinters() {
		try {
			// list_remote_printers returns all enabled printers; filter by our hub_id
			const all = await call("pos_next.api.remote_print.list_remote_printers", {
				include_offline: 1,
			})
			const normalized = all?.message || all || []
			sharedPrinters.value = normalized.filter((p) => p.hub_id === hubId.value)
		} catch (error) {
			log.error("Failed to refresh shared printers:", error?.message || error)
		}
	}

	/**
	 * Start the heartbeat loop to keep shared printers online.
	 */
	function startHeartbeat() {
		if (isHeartbeatRunning.value) return
		isHeartbeatRunning.value = true

		// Send immediately, then on interval
		sendHeartbeat()
		heartbeatTimer = setInterval(sendHeartbeat, HEARTBEAT_INTERVAL_MS)
		log.info("Heartbeat started")
	}

	/**
	 * Stop the heartbeat loop.
	 */
	function stopHeartbeat() {
		if (heartbeatTimer) {
			clearInterval(heartbeatTimer)
			heartbeatTimer = null
		}
		isHeartbeatRunning.value = false
		log.info("Heartbeat stopped")
	}

	async function sendHeartbeat() {
		if (sharedPrinters.value.length === 0) return
		try {
			await call("pos_next.api.remote_print.heartbeat_remote_printers", {
				hub_id: hubId.value,
			})
		} catch (error) {
			log.warn("Heartbeat failed:", error?.message || error)
		}
	}

	/**
	 * Start the hub: connect QZ Tray, start heartbeat, listen for print jobs.
	 */
	async function startHub() {
		if (isHubActive.value) return

		// Ensure QZ Tray is connected — we need it to actually print
		if (!qzConnected.value) {
			const ok = await qzConnect()
			if (!ok) {
				log.warn("Cannot start hub — QZ Tray not connected")
				return false
			}
		}

		startHeartbeat()
		startListening()
		isHubActive.value = true
		log.info("Print hub started", { hubId: hubId.value })
		return true
	}

	/**
	 * Stop the hub: stop heartbeat, stop listening for jobs.
	 */
	function stopHub() {
		stopHeartbeat()
		stopListening()
		isHubActive.value = false
		log.info("Print hub stopped")
	}

	/**
	 * Listen for incoming print job events via Socket.IO.
	 */
	function startListening() {
		if (!window.frappe?.realtime) {
			log.warn("Socket.IO not available — cannot listen for print jobs")
			return
		}
		window.frappe.realtime.on(PRINT_JOB_EVENT, handlePrintJobEvent)
		log.info("Listening for remote print jobs")
	}

	function stopListening() {
		if (window.frappe?.realtime) {
			window.frappe.realtime.off(PRINT_JOB_EVENT, handlePrintJobEvent)
		}
	}

	/**
	 * Handle an incoming print job event.
	 * Only processes jobs targeted at this hub's printers.
	 */
	async function handlePrintJobEvent(data) {
		if (!data || !data.job) return

		// Only handle jobs for this hub
		if (data.hub_id !== hubId.value) return

		// Avoid duplicate processing
		if (activeJobs.value.has(data.job)) return

		activeJobs.value.set(data.job, { status: "received", ...data })

		try {
			await processPrintJob(data)
		} catch (error) {
			log.error(
				`Failed to process print job ${data.job}:`,
				error?.message || error,
			)
			await reportJobFailed(data.job, error?.message || "Unknown error")
		} finally {
			activeJobs.value.delete(data.job)
		}
	}

	/**
	 * Claim a job, fetch its payload, print it, and report the result.
	 */
	async function processPrintJob(jobData) {
		// 1. Claim the job (atomic — prevents duplicate printing)
		const claimResult = await call(
			"pos_next.api.remote_print.claim_remote_print_job",
			{
				job_name: jobData.job,
				hub_id: hubId.value,
			},
		)
		const claim = claimResult?.message || claimResult
		if (!claim?.claimed) {
			log.debug(
				`Job ${jobData.job} already claimed or not ours (status: ${claim?.status})`,
			)
			return
		}

		// 2. Fetch only routing metadata. The hub uses the same local print
		// utilities as direct QZ printing so raw/HTML format decisions stay identical.
		const payloadResult = await call(
			"pos_next.api.remote_print.get_remote_print_job_payload",
			{
				job_name: jobData.job,
			},
		)
		const payload = payloadResult?.message || payloadResult
		if (!payload) {
			throw new Error("No payload returned for print job")
		}

		// 3. Print via QZ Tray using the local print path
		await ensureQzConnected()

		const printerName = payload.qz_printer_name
		if (!printerName) {
			throw new Error("No printer name in payload")
		}

		if (payload.job_type === "Invoice") {
			await printWithSilentFallback(
				{ name: payload.reference_name, doctype: payload.reference_doctype },
				null,
				printerName,
			)
			log.info(`Invoice print job ${jobData.job} sent to "${printerName}"`)
		} else if (payload.job_type === "Closing Report") {
			const result = await printClosingReportWithFallback(
				payload.reference_name,
				true,
				printerName,
			)
			if (!result.success) {
				throw new Error("Closing report print failed")
			}
			log.info(`Closing report print job ${jobData.job} sent to "${printerName}"`)
		} else {
			throw new Error(`Unsupported remote print job type: ${payload.job_type}`)
		}

		// 4. Report success
		await call("pos_next.api.remote_print.complete_remote_print_job", {
			job_name: jobData.job,
		})
		log.success(`Print job ${jobData.job} completed`)
	}

	async function reportJobFailed(jobName, errorMessage) {
		try {
			await call("pos_next.api.remote_print.fail_remote_print_job", {
				job_name: jobName,
				error_message: errorMessage,
			})
		} catch (err) {
			log.error("Failed to report job failure:", err?.message || err)
		}
	}

	async function ensureQzConnected() {
		if (qzConnected.value) return
		const ok = await qzConnect()
		if (!ok) throw new Error("QZ Tray is not connected")
	}

	// ========================================================================
	// SENDER SIDE — Listing printers & creating print jobs
	// ========================================================================

	/**
	 * Fetch available remote printers for selection.
	 * @param {string} posProfile - Current POS Profile (for scoping)
	 * @param {string} [allowedType] - Optional filter by type
	 */
	async function loadAvailablePrinters(posProfile, allowedType = null) {
		isLoadingPrinters.value = true
		try {
			const result = await call(
				"pos_next.api.remote_print.list_remote_printers",
				{
					pos_profile: posProfile || "",
					allowed_type: allowedType || "",
					include_offline: 1,
				},
			)
			availablePrinters.value = result?.message || result || []
		} catch (error) {
			log.error("Failed to load available printers:", error?.message || error)
			availablePrinters.value = []
		} finally {
			isLoadingPrinters.value = false
		}
	}

	/**
	 * Create a remote print job (sender side).
	 * @param {Object} params
	 * @param {string} params.remotePrinter - POS Remote Printer name/id
	 * @param {string} params.jobType - Invoice | Closing Report
	 * @param {string} params.referenceDoctype - e.g. "Sales Invoice"
	 * @param {string} params.referenceName - Document name/id
	 * @returns {Promise<Object>} Job creation result
	 */
	async function createPrintJob({
		remotePrinter,
		jobType,
		referenceDoctype,
		referenceName,
	}) {
		try {
			const result = await call(
				"pos_next.api.remote_print.create_remote_print_job",
				{
					remote_printer: remotePrinter,
					job_type: jobType,
					reference_doctype: referenceDoctype,
					reference_name: referenceName,
				},
			)
			log.info(`Remote print job created: ${jobType} → ${remotePrinter}`)
			return result?.message || result
		} catch (error) {
			log.error("Failed to create remote print job:", error?.message || error)
			throw error
		}
	}

	// ========================================================================
	// CLEANUP
	// ========================================================================

	function dispose() {
		stopHub()
		availablePrinters.value = []
		sharedPrinters.value = []
	}

	return {
		// State
		hubId,
		sharedPrinters,
		availablePrinters,
		isHubActive,
		isHeartbeatRunning,
		isLoadingPrinters,
		activeJobs,

		// Computed
		hasSharedPrinters,
		onlinePrinters,

		// Helpers
		isLocalPrinter,

		// Hub actions
		registerPrinter,
		unregisterPrinter,
		refreshSharedPrinters,
		startHub,
		stopHub,
		startHeartbeat,
		stopHeartbeat,

		// Sender actions
		loadAvailablePrinters,
		createPrintJob,

		// Cleanup
		dispose,
	}
})
