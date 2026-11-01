import { useEffect, useRef, useState } from "react";
import { ApiError, errorMessage, get, post } from "../lib/api";
import { dictationSupported, speak, speechSupported, startDictation, stopSpeaking } from "../lib/speech";
import { readLocal, writeLocal } from "../lib/storage";
import { Button, ErrorNote, Note } from "./ui";

const MODES = [
  ["ASK", "Ask"],
  ["EXPLAIN", "Explain simply"],
  ["EXAMPLE", "Give an example"],
  ["HINT", "Give me a hint"],
  ["PRACTICE", "Quiz me"],
] as const;

interface Msg {
  role: "STUDENT" | "TUTOR";
  content: string;
  off_topic?: boolean;
}

export default function TutorPanel({ lessonId, studentAssignmentId }: { lessonId: string; studentAssignmentId: string }) {
  const [messages, setMessages] = useState<Msg[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [paused, setPaused] = useState<string | null>(null);
  const [listening, setListening] = useState(false);
  const [voiceInput, setVoiceInput] = useState(false);
  const [readAloud, setReadAloud] = useState(readLocal("aayra.readAloud") === "1");
  const stopRef = useRef<() => void>(() => {});
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    get<any>(`/v1/lessons/${lessonId}/tutor/conversations`)
      .then((d) => {
        const c = d.conversations?.[0];
        if (c) {
          setConversationId(c.conversation_id);
          setMessages(c.messages.map((m: any) => ({ role: m.role, content: m.content, off_topic: m.off_topic })));
        }
      })
      .catch(() => {});
    return () => stopSpeaking();
  }, [lessonId]);

  useEffect(() => endRef.current?.scrollIntoView({ block: "end" }), [messages.length]);

  async function send(mode: string, message?: string) {
    const content = (message ?? text).trim() || (mode === "PRACTICE" ? "Quiz me on this lesson." : mode === "EXAMPLE" ? "Give me an example." : mode === "EXPLAIN" ? "Explain this lesson simply." : mode === "HINT" ? "Give me a hint." : "");
    if (!content) return;
    setBusy(true);
    setError(null);
    setMessages((m) => [...m, { role: "STUDENT", content }]);
    setText("");
    try {
      const r = await post<any>(`/v1/lessons/${lessonId}/tutor`, {
        student_assignment_id: studentAssignmentId,
        message: content,
        mode,
        conversation_id: conversationId,
        input_mode: voiceInput ? "VOICE" : "TEXT",
      });
      setConversationId(r.conversation_id);
      setMessages((m) => [...m, { role: "TUTOR", content: r.reply, off_topic: r.off_topic }]);
      if (readAloud) speak(r.reply);
    } catch (e) {
      setMessages((m) => m.slice(0, -1));
      setText(content);
      if (e instanceof ApiError && e.code === "TUTOR_PAUSED_DURING_CHECK") setPaused(e.message);
      else setError(errorMessage(e));
    } finally {
      setBusy(false);
      setVoiceInput(false);
    }
  }

  function toggleMic() {
    if (listening) {
      stopRef.current();
      return;
    }
    setListening(true);
    setVoiceInput(true);
    stopRef.current = startDictation(
      (t) => setText(t),
      () => setListening(false),
    );
  }

  return (
    <aside className="tutor" aria-label="AI tutor">
      <div className="tutor-head">
        <strong>Tutor</strong>
        <span className="muted small">Helps with this lesson only</span>
        {speechSupported() ? (
          <label className="check small">
            <input
              type="checkbox"
              checked={readAloud}
              onChange={(e) => {
                setReadAloud(e.target.checked);
                writeLocal("aayra.readAloud", e.target.checked ? "1" : "0");
                if (!e.target.checked) stopSpeaking();
              }}
            />
            Read replies aloud
          </label>
        ) : null}
      </div>
      {paused ? <Note tone="info">{paused}</Note> : null}
      <div className="tutor-log">
        {!messages.length ? <p className="muted small">Stuck on something? Ask a question, or tap a button below.</p> : null}
        {messages.map((m, i) => (
          <div key={i} className={`bubble ${m.role === "STUDENT" ? "me" : "ai"} ${m.off_topic ? "off" : ""}`}>
            {m.content}
            {m.role === "TUTOR" && speechSupported() ? (
              <button className="link small" onClick={() => speak(m.content)} aria-label="Read aloud">🔊</button>
            ) : null}
          </div>
        ))}
        {busy ? <div className="bubble ai muted">Thinking...</div> : null}
        <div ref={endRef} />
      </div>
      <div className="chips">
        {MODES.slice(1).map(([m, t]) => (
          <button key={m} className="chip" disabled={busy || !!paused} onClick={() => send(m)}>{t}</button>
        ))}
      </div>
      <form
        className="tutor-input"
        onSubmit={(e) => {
          e.preventDefault();
          send("ASK");
        }}
      >
        <input value={text} onChange={(e) => setText(e.target.value)} placeholder={listening ? "Listening..." : "Ask about this lesson"} disabled={!!paused} aria-label="Message the tutor" />
        {dictationSupported() ? (
          <button type="button" className={`mic ${listening ? "on" : ""}`} onClick={toggleMic} aria-label={listening ? "Stop dictation" : "Speak your question"} disabled={!!paused}>
            🎤
          </button>
        ) : null}
        <Button type="submit" busy={busy} disabled={!text.trim() || !!paused}>Send</Button>
      </form>
      <ErrorNote error={error} />
    </aside>
  );
}
