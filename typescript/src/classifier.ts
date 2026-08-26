import type { ClassifierRule } from "./classifierRules.js";
import { CLASSIFIER_RULES } from "./classifierRules.js";
import { FailureClass, isRetryable, toFailureClass } from "./taxonomy.js";

export interface ClassifyResult {
  failureClass: FailureClass;
  retryable: boolean;
}

export class ClassifierMapper {
  private readonly rules: readonly ClassifierRule[];

  constructor(rules: readonly ClassifierRule[] = CLASSIFIER_RULES) {
    this.rules = [...rules].sort((a, b) => b.priority - a.priority);
  }

  classify(
    errorCode: string,
    errorDescription?: string | null,
    errorSource?: string | null,
    errorStep?: string | null,
    errorReason?: string | null,
  ): ClassifyResult {
    for (const rule of this.rules) {
      if (!ClassifierMapper.matches(rule, errorCode, errorSource, errorStep, errorReason)) {
        continue;
      }
      const fc = toFailureClass(rule.failure_class);
      if (fc === null) continue;
      return { failureClass: fc, retryable: rule.retryable ?? isRetryable(fc) };
    }
    return { failureClass: FailureClass.Unknown, retryable: false };
  }

  private static matches(
    rule: ClassifierRule,
    errorCode: string,
    errorSource: string | null | undefined,
    errorStep: string | null | undefined,
    errorReason: string | null | undefined,
  ): boolean {
    if (rule.error_reason !== undefined) {
      if (!errorReason || rule.error_reason !== errorReason) return false;
    }
    if (rule.error_step !== undefined) {
      if (!errorStep || rule.error_step !== errorStep) return false;
    }
    if (rule.error_source !== undefined) {
      if (!errorSource || rule.error_source !== errorSource) return false;
    }
    if (rule.error_code !== undefined) {
      if (rule.error_code !== errorCode) return false;
    }
    return true;
  }
}
