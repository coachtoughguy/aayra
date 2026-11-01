import { useState } from "react";
import { errorMessage, post, uploadToSignedUrl } from "../lib/api";
import { contentTypeFor } from "../lib/format";
import { Button, ErrorNote, Field, Note } from "./ui";

export interface AddedItem {
  content_item_id: string;
  title: string;
  content_type: string;
}

interface Props {
  purpose: "SCHOOL" | "PARENT" | "SELF";
  studentId?: string;
  onAdded: (item: AddedItem) => void;
}

const ACCEPT = ".pdf,.docx,image/*,video/*,audio/*";

export default function ContentUploader({ purpose, studentId, onAdded }: Props) {
  const [tab, setTab] = useState<"file" | "youtube" | "link">("file");
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [url, setUrl] = useState("");
  const [title, setTitle] = useState("");
  const [transcript, setTranscript] = useState("");

  const base = { purpose, ...(purpose === "PARENT" ? { student_id: studentId } : {}) };

  async function uploadFiles(files: FileList | null) {
    if (!files?.length) return;
    setBusy(true);
    setError(null);
    try {
      for (const file of Array.from(files)) {
        const ctype = contentTypeFor(file);
        if (ctype === "OTHER") throw new Error(`${file.name}: use PDF, Word (.docx), an image, a video or audio file.`);
        if (file.name.toLowerCase().endsWith(".doc")) throw new Error(`${file.name}: save old .doc files as .docx or PDF first.`);
        setProgress(`Uploading ${file.name}...`);
        const item = await post<any>("/v1/content-items", { ...base, content_type: ctype, title: file.name.replace(/\.[^.]+$/, "") });
        await uploadToSignedUrl(item.upload.upload_url, file);
        await post(`/v1/content-items/${item.content_item_id}/complete-upload`, {
          object_key: item.upload.object_key,
          mime_type: file.type || "application/octet-stream",
          file_size_bytes: file.size,
        });
        onAdded({ content_item_id: item.content_item_id, title: item.title, content_type: ctype });
      }
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
      setProgress(null);
    }
  }

  async function addLink(kind: "YOUTUBE" | "WEB_LINK") {
    setBusy(true);
    setError(null);
    try {
      const body: any = { ...base, content_type: kind, source_url: url.trim(), title: title.trim() || null };
      if (kind === "YOUTUBE") {
        if (transcript.trim().length < 20) throw new Error("Paste the video's transcript (or load a .vtt/.srt file).");
        body.transcript_text = transcript;
      }
      const item = await post<any>("/v1/content-items", body);
      onAdded({ content_item_id: item.content_item_id, title: item.title || url, content_type: kind });
      setUrl("");
      setTitle("");
      setTranscript("");
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="uploader">
      <div className="seg" role="tablist">
        {(["file", "youtube", "link"] as const).map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? "on" : ""} onClick={() => setTab(t)} type="button">
            {t === "file" ? "Upload files" : t === "youtube" ? "YouTube video" : "Web page"}
          </button>
        ))}
      </div>
      {tab === "file" ? (
        <label className={`drop ${busy ? "busy" : ""}`}>
          <input type="file" multiple accept={ACCEPT} disabled={busy} onChange={(e) => uploadFiles(e.target.files)} />
          <strong>{progress || "Choose files"}</strong>
          <span className="muted small">PDF, Word (.docx), images/photos, video or audio</span>
        </label>
      ) : null}
      {tab === "youtube" ? (
        <div className="stack">
          <Field label="YouTube link"><input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://www.youtube.com/watch?v=..." /></Field>
          <Field label="Title (optional)"><input value={title} onChange={(e) => setTitle(e.target.value)} /></Field>
          <Field label="Transcript" hint="On YouTube: ... (More) > Show transcript, then copy it here. Or load a .vtt / .srt / .txt file.">
            <textarea rows={6} value={transcript} onChange={(e) => setTranscript(e.target.value)} />
          </Field>
          <input
            type="file"
            accept=".vtt,.srt,.txt,text/plain"
            onChange={async (e) => {
              const f = e.target.files?.[0];
              if (f) setTranscript(await f.text());
            }}
          />
          <Note>Aayra doesn't download YouTube videos. It learns from the transcript you provide.</Note>
          <Button type="button" busy={busy} onClick={() => addLink("YOUTUBE")} disabled={!url}>Add video</Button>
        </div>
      ) : null}
      {tab === "link" ? (
        <div className="stack">
          <Field label="Web page link"><input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://..." /></Field>
          <Field label="Title (optional)"><input value={title} onChange={(e) => setTitle(e.target.value)} /></Field>
          <Button type="button" busy={busy} onClick={() => addLink("WEB_LINK")} disabled={!url}>Add page</Button>
        </div>
      ) : null}
      <ErrorNote error={error} />
    </div>
  );
}
