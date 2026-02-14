"use client";

import { useState } from "react";
import { Check, ArrowRightLeft, Download, Loader2, Gavel, Award, FileText } from "lucide-react";
import { submitDecision } from "@/lib/api";
import type { ReviewResponse, DecisionResponse } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

interface Props {
  review: ReviewResponse;
}

type Mode = "initial" | "override" | "submitted";

export function DecisionPanel({ review }: Props) {
  const [mode, setMode] = useState<Mode>("initial");
  const [reviewerName, setReviewerName] = useState("");
  const [overrideRec, setOverrideRec] = useState<"approve" | "pend_for_review">(
    review.recommendation === "approve" ? "pend_for_review" : "approve"
  );
  const [overrideRationale, setOverrideRationale] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [decision, setDecision] = useState<DecisionResponse | null>(null);

  const handleAccept = async () => {
    if (!reviewerName.trim()) {
      setError("Reviewer name is required");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const resp = await submitDecision({
        request_id: review.request_id,
        action: "accept",
        reviewer_name: reviewerName.trim(),
      });
      setDecision(resp);
      setMode("submitted");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Decision failed");
    } finally {
      setLoading(false);
    }
  };

  const handleOverrideSubmit = async () => {
    if (!reviewerName.trim()) {
      setError("Reviewer name is required");
      return;
    }
    if (!overrideRationale.trim()) {
      setError("Override rationale is required");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const resp = await submitDecision({
        request_id: review.request_id,
        action: "override",
        override_recommendation: overrideRec,
        override_rationale: overrideRationale.trim(),
        reviewer_name: reviewerName.trim(),
      });
      setDecision(resp);
      setMode("submitted");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Decision failed");
    } finally {
      setLoading(false);
    }
  };

  const handleDownload = () => {
    if (!decision) return;
    if (decision.letter.pdf_base64) {
      const byteChars = atob(decision.letter.pdf_base64);
      const byteNumbers = new Uint8Array(byteChars.length);
      for (let i = 0; i < byteChars.length; i++) {
        byteNumbers[i] = byteChars.charCodeAt(i);
      }
      const blob = new Blob([byteNumbers], { type: "application/pdf" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${decision.authorization_number}.pdf`;
      a.click();
      URL.revokeObjectURL(url);
    } else {
      const blob = new Blob([decision.letter.body_text], {
        type: "text/plain",
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${decision.authorization_number}.txt`;
      a.click();
      URL.revokeObjectURL(url);
    }
  };

  if (mode === "submitted" && decision) {
    return (
      <Card className="mt-6 bg-muted/30 shadow-sm">
        <CardHeader className="pb-3">
          <CardTitle className="text-base flex items-center gap-2">
            <Award className="h-5 w-5 text-green-600" />
            Decision Recorded
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center gap-2">
            <Badge variant="success" className="text-sm px-3 py-1.5">
              Auth #: {decision.authorization_number}
            </Badge>
          </div>
          <div className="text-sm">
            <span className="font-semibold">Final Recommendation:</span>{" "}
            {decision.final_recommendation.replace(/_/g, " ").toUpperCase()}
            {decision.was_overridden && (
              <span className="ml-2 text-amber-600">(overridden)</span>
            )}
          </div>
          <div>
            <p className="text-sm font-medium mb-2 flex items-center gap-1.5">
              <FileText className="h-3.5 w-3.5 text-muted-foreground" />
              Notification Letter
            </p>
            <ScrollArea className="h-[300px] rounded-md border bg-card p-4">
              <pre className="whitespace-pre-wrap font-mono text-xs">
                {decision.letter.body_text}
              </pre>
            </ScrollArea>
          </div>
          <div className="flex items-center gap-3">
            <Button onClick={handleDownload} className="bg-gradient-to-r from-[#0078D4] to-[#005A9E] hover:from-[#006CBD] hover:to-[#004E8C] text-white shadow-sm">
              <Download className="mr-2 h-4 w-4" />
              Download Letter
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">
            Decided by: {decision.decided_by} | {decision.decided_at}
          </p>
        </CardContent>
      </Card>
    );
  }

  if (mode === "override") {
    return (
      <Card className="mt-6 bg-muted/30 shadow-sm">
        <CardHeader className="pb-3">
          <CardTitle className="text-base flex items-center gap-2">
            <ArrowRightLeft className="h-5 w-5 text-amber-500" />
            Override Recommendation
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="reviewer_override">Reviewer Name</Label>
            <Input
              id="reviewer_override"
              value={reviewerName}
              onChange={(e) => setReviewerName(e.target.value)}
              placeholder="Dr. Jane Doe"
            />
          </div>
          <div className="space-y-2">
            <Label>New Recommendation</Label>
            <Select
              value={overrideRec}
              onValueChange={(v) =>
                setOverrideRec(v as "approve" | "pend_for_review")
              }
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="approve">Approve</SelectItem>
                <SelectItem value="pend_for_review">Pend for Review</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-2">
            <Label htmlFor="rationale">Override Rationale (required)</Label>
            <Textarea
              id="rationale"
              value={overrideRationale}
              onChange={(e) => setOverrideRationale(e.target.value)}
              placeholder="Explain why you are overriding the AI recommendation..."
              className="min-h-[80px]"
            />
          </div>
          <div className="flex flex-wrap gap-2">
            <Button
              onClick={handleOverrideSubmit}
              disabled={loading}
              className="bg-amber-500 text-white hover:bg-amber-600"
            >
              {loading && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              {loading ? "Submitting..." : "Submit Override"}
            </Button>
            <Button
              variant="ghost"
              onClick={() => {
                setMode("initial");
                setError(null);
              }}
              disabled={loading}
            >
              Cancel
            </Button>
          </div>
          {error && (
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="mt-6 bg-muted/30 shadow-sm">
      <CardHeader className="pb-3">
        <CardTitle className="text-base flex items-center gap-2">
          <Gavel className="h-5 w-5 text-primary" />
          Reviewer Decision
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-2">
          <Label htmlFor="reviewer_name">Reviewer Name</Label>
          <Input
            id="reviewer_name"
            value={reviewerName}
            onChange={(e) => setReviewerName(e.target.value)}
            placeholder="Dr. Jane Doe"
          />
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            onClick={handleAccept}
            disabled={loading}
            className="bg-green-600 text-white hover:bg-green-700"
          >
            {loading ? (
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            ) : (
              <Check className="mr-2 h-4 w-4" />
            )}
            {loading ? "Submitting..." : "Accept Recommendation"}
          </Button>
          <Button
            variant="secondary"
            onClick={() => {
              setMode("override");
              setError(null);
            }}
            disabled={loading}
            className="border border-amber-300 bg-amber-100 text-amber-900 hover:bg-amber-200"
          >
            <ArrowRightLeft className="mr-2 h-4 w-4" />
            Override
          </Button>
        </div>
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
      </CardContent>
    </Card>
  );
}
