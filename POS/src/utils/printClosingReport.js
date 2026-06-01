import { call } from "@/utils/apiWrapper"
import { logger } from "@/utils/logger"
import {
	printHTML as qzPrintHTML,
	printRawCommands as qzPrintRawCommands,
} from "@/utils/qzTray"

const log = logger.create("PrintClosingReport")

async function getClosingReportPayload(closingShiftName) {
	if (!closingShiftName) {
		throw new Error(__("Closing shift name is required"))
	}

	const result = await call(
		"pos_next.api.shifts.get_closing_shift_print_html",
		{
			closing_shift: closingShiftName,
		},
	)
	const html = result?.message || result
	if (!html) {
		throw new Error(__("Failed to load closing report"))
	}

	if (typeof html === "string") {
		return { type: "html", html }
	}

	return html
}

function withBrowserControls(html) {
	const controls = `
		<div class="no-print" style="text-align: center; margin-top: 20px;">
			<button onclick="window.print()" style="padding: 10px 20px; font-size: 14px; cursor: pointer;">${__("Print Report")}</button>
			<button onclick="window.close()" style="padding: 10px 20px; font-size: 14px; cursor: pointer; margin-left: 10px;">${__("Close")}</button>
		</div>
	`
	return html.includes("</body>")
		? html.replace("</body>", `${controls}</body>`)
		: `${html}${controls}`
}

function openBrowserPrintWindow(html) {
	const printWindow = window.open("", "_blank", "width=420,height=700")
	if (!printWindow) {
		throw new Error(__("Popup blocked - check your browser settings."))
	}

	printWindow.document.write(withBrowserControls(html))
	printWindow.document.close()
	printWindow.onload = () => {
		setTimeout(() => printWindow.print(), 250)
	}
	return true
}

export async function browserPrintClosingReport(closingShiftName) {
	const payload = await getClosingReportPayload(closingShiftName)
	const html = payload.type === "raw" ? payload.fallback_html : payload.html
	if (!html) {
		throw new Error(__("Closing report cannot be printed in the browser"))
	}

	return openBrowserPrintWindow(html)
}

export async function silentPrintClosingReport(payload, closingShiftName) {
	if (payload.type === "raw") {
		if (!payload.raw_commands) {
			throw new Error(__("Raw closing report commands are missing"))
		}
		await qzPrintRawCommands(payload.raw_commands)
		log.info(`Raw silent closing report print sent for ${closingShiftName}`)
		return true
	}

	if (!payload.html) {
		throw new Error(__("Closing report HTML is missing"))
	}
	await qzPrintHTML(payload.html)
	log.info(`Silent closing report print sent for ${closingShiftName}`)
	return true
}

export async function printClosingReportWithFallback(
	closingShiftName,
	silentPrintEnabled = false,
) {
	const payload = await getClosingReportPayload(closingShiftName)

	if (silentPrintEnabled) {
		try {
			await silentPrintClosingReport(payload, closingShiftName)
			return { method: "silent", success: true }
		} catch (error) {
			log.warn(
				"Silent closing report print failed, falling back to browser:",
				error?.message || error,
			)
		}
	}

	try {
		const html = payload.type === "raw" ? payload.fallback_html : payload.html
		if (!html) {
			throw new Error(__("Closing report cannot be printed in the browser"))
		}
		openBrowserPrintWindow(html)
		return { method: "browser", success: true }
	} catch (error) {
		log.error("Browser closing report print failed:", error?.message || error)
		return { method: "browser", success: false }
	}
}
