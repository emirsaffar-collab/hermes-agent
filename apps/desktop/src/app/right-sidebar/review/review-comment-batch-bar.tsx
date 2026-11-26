import { useStore } from '@nanostores/react'

import { requestComposerSubmit } from '@/app/chat/composer/focus'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { useI18n } from '@/i18n'
import { packageDiffComments } from '@/lib/review-comments/pack'
import { notifyError } from '@/store/notifications'
import { $reviewScopeCwd, $reviewScopeTarget } from '@/store/review'
import { $reviewComments, clearRepoComments, normalizeRepoKey } from '@/store/review-comments'
import { $currentCwd } from '@/store/session'

// Sticky batch bar at the bottom of the Review pane whenever at least one
// inline diff comment is queued for the active repo/worktree. Packs all
// comments deterministically and hands them to the owning chat composer.
export function ReviewCommentBatchBar() {
  const { t } = useI18n()
  const c = t.statusStack.coding
  const allComments = useStore($reviewComments)
  const scopeCwd = useStore($reviewScopeCwd)
  const scopeTarget = useStore($reviewScopeTarget)
  const currentCwd = useStore($currentCwd)

  const repoCwd = scopeCwd || currentCwd
  const repoKey = normalizeRepoKey(repoCwd)
  const comments = repoKey ? (allComments[repoKey] ?? []) : []

  if (comments.length === 0) {
    return null
  }

  const fileCount = new Set(comments.map(item => item.filePath)).size
  const staleCount = comments.filter(item => item.stale).length

  const handleSend = () => {
    const prompt = packageDiffComments(comments, repoCwd ?? '')

    if (!prompt) {
      return
    }

    if (!requestComposerSubmit(prompt, { target: scopeTarget })) {
      notifyError(new Error(c.agentShipUnavailable), c.sendCommentsToChat)

      return
    }

    clearRepoComments(repoCwd ?? '')
  }

  return (
    <div
      className="flex shrink-0 flex-wrap items-center justify-between gap-1.5 border-t border-(--ui-stroke-secondary) bg-(--ui-surface-secondary)/80 px-2.5 py-1.5"
      data-suppress-pane-reveal-side=""
      data-testid="review-comment-batch-bar"
    >
      <div className="flex min-w-0 items-center gap-1.5 text-[0.68rem] text-(--ui-text-secondary)">
        <Codicon className="shrink-0 text-(--ui-text-tertiary)" name="comment-discussion" size="0.8rem" />
        <span className="truncate font-medium">{c.commentsBatchSummary(comments.length, fileCount)}</span>
        {staleCount > 0 && (
          <span
            className="shrink-0 rounded bg-amber-500/15 px-1 py-px text-[0.6rem] font-medium text-amber-500"
            data-testid="review-comment-batch-stale"
          >
            {staleCount} {c.commentStale}
          </span>
        )}
      </div>

      <div className="flex shrink-0 items-center gap-1">
        <Button
          className="h-6 px-2 text-[0.68rem] text-muted-foreground hover:text-foreground"
          onClick={() => clearRepoComments(repoCwd ?? '')}
          size="sm"
          variant="ghost"
        >
          {c.clearComments}
        </Button>
        <Button className="h-6 gap-1 px-2 text-[0.68rem]" onClick={handleSend} size="sm" variant="default">
          <Codicon name="send" size="0.72rem" />
          <span>{c.sendCommentsToChat}</span>
        </Button>
      </div>
    </div>
  )
}
