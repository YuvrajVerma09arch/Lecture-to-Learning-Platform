/**
 * The languages the voice session can be set to.
 *
 * Gnani's streaming STT takes `lang_code` as an instruction, not a
 * detector: it forces every utterance into that language's script
 * (`scripts/probe_lang_detect.py`, findings §1.7 of
 * `docs/live-api-findings.md`). There is no auto-detect — comma lists
 * close the socket with 1008 — so the session language is chosen here,
 * once, before the room opens.
 *
 * Codes must stay in sync with `SUPPORTED_LANGUAGES` in
 * `backend/app/api/livekit.py` (the backend rejects anything else with
 * a 422) and with `GnaniSTTLanguages` in the plugin.
 */
export interface SessionLanguage {
  /** The `lang_code` sent to Gnani, e.g. `hi-IN`. */
  code: string;
  /** Native-script label so the student recognises their language. */
  label: string;
}

export const SESSION_LANGUAGES: readonly SessionLanguage[] = [
  { code: "en-IN", label: "English" },
  { code: "hi-IN", label: "हिन्दी" },
  { code: "bn-IN", label: "বাংলা" },
  { code: "te-IN", label: "తెలుగు" },
  { code: "ta-IN", label: "தமிழ்" },
  { code: "gu-IN", label: "ગુજરાતી" },
  { code: "pa-IN", label: "ਪੰਜਾਬੀ" },
  { code: "mr-IN", label: "मराठी" },
  { code: "ml-IN", label: "മലയാളം" },
  { code: "kn-IN", label: "ಕನ್ನಡ" },
];

/** A language code is selectable only if the backend whitelist has it. */
export function isSupportedLanguage(code: unknown): code is string {
  return (
    typeof code === "string" &&
    SESSION_LANGUAGES.some((language) => language.code === code)
  );
}

/** The session default: the lecture's language when it is supported. */
export function defaultLanguage(lectureLanguage?: string): string {
  return isSupportedLanguage(lectureLanguage) ? lectureLanguage : "en-IN";
}
