#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查器接口与通用校验。

每个检查器只声明：认什么对象、生成哪些计划项、冷跑报什么、清单怎么绑定。
计划项结构、证据契约、准出语义仍由既有流程决定，检查器不自造结论。
"""


class Checker:
    """检查器基类。子类覆盖需要的钩子，其余保持默认的"不适用"。"""

    id = ''
    title = ''
    plan_key = None          # 清单在计划 JSON 中的顶层键
    version_key = None       # 版本字段名（可选）
    version = None
    intent_key = None        # intent 中的配置段
    cold_rules = {}          # 规则号 -> 名称
    hot_rules = {}           # 规则号 -> 名称
    evidence_kinds = {}      # 热跑规则号 -> 允许的 kind 集合

    generated_fields = ('check', 'rule', 'object', 'criterion', 'stage', 'executor')
    allow_manual_bound_objects = True

    @property
    def missing_inventory_message(self):
        return '%s checks require their inventory' % self.id

    @property
    def inventory_type_message(self):
        return '%s inventory must be an object' % self.id

    @property
    def requires_db_message(self):
        return '%s inventory validation requires --db' % self.id

    @property
    def stale_message(self):
        return '%s inventory/binding is stale or modified' % self.id

    @property
    def invalid_message(self):
        return 'invalid %s inventory: ' % self.id

    def incomplete_message(self, key):
        return key + ': incomplete %s planned coverage' % self.id

    def changed_message(self, key):
        return key + ': %s generated criterion/object changed' % self.id

    def manual_addition_message(self, key):
        return key + ': use an independent object for manual %s additions' % self.id

    # -- 钩子 ---------------------------------------------------------
    def validate_intent(self, intent, db):
        """校验 intent 中本检查器的配置段，返回错误文案列表。"""
        return []

    def build(self, db, intent):
        """生成确定性清单；返回 None 表示本检查器不产出清单。"""
        return None

    def plan(self, planner, inventory):
        """通过 planner.add_check 生成计划项。"""

    def cold_findings(self, lint, inventory):
        """通过 lint.add 输出冷跑发现。"""

    def hot_check(self, lint, check):
        """执行本检查器的热跑规则（证据已就绪）。"""
        raise NotImplementedError(self.id + ': hot rule handler missing')

    def evidence_errors(self, check, label):
        """校验本检查器热跑证据的结构；缺字段属结构错误，数值缺口走 model_gaps。"""
        return []

    def model_gaps(self, check):
        """缺哪些保证值就不能热跑；返回非空即 INSUFFICIENT，不得用典型值顶替。"""
        return []

    def rule_instances(self, rule, inventory):
        """该规则本次扫描覆盖到的对象标识。"""
        return []

    def binds(self, item):
        """计划项是否由本检查器生成或绑定到其清单。"""
        return False

    def extra_presence_trigger(self, plan):
        """除绑定项外，还有什么迹象表明本检查器的清单必须存在。"""
        return False

    def object_errors(self, key, obj, inventory):
        return []

    def pass_blockers(self, key, planned, generated, inventory):
        """返回该项判 PASS 时的阻断理由。"""
        return []

    def context_intent(self, inventory):
        """从清单里取回重建所需的上下文，用于过期检测。"""
        if not isinstance(inventory, dict) or self.intent_key is None:
            return None
        context = inventory.get('context')
        return {self.intent_key: context} if context is not None else None


def validate_inventories(registry, plan, expected, db, require, planner_factory):
    """通用清单校验：存在性、版本、过期、生成项一致、对象绑定。

    返回 {check_id: [PASS 阻断理由]}，由调用方在结果为 PASS 时使用。
    """
    gates = {}
    for checker in registry:
        if checker.plan_key is None:
            continue
        bound = [item for item in expected.values() if checker.binds(item)]
        present = checker.plan_key in plan
        require(present or not (bound or checker.extra_presence_trigger(plan)),
                checker.missing_inventory_message)
        if not present:
            continue
        if checker.version_key is not None:
            require(type(plan.get(checker.version_key)) is int
                    and plan[checker.version_key] == checker.version,
                    '%s must be %s' % (checker.version_key, checker.version))
        inventory = plan[checker.plan_key]
        require(isinstance(inventory, dict), checker.inventory_type_message)
        if not isinstance(inventory, dict):
            continue
        if db is None:
            require(False, checker.requires_db_message)
            continue
        try:
            planner = planner_factory(checker.context_intent(inventory))
            current = planner.inventories.get(checker.id)
            require(current == inventory, checker.stale_message)
            checker.plan(planner, current)
            generated = {item['id']: item for item in planner.checks}
            for key, item in generated.items():
                planned = expected.get(key)
                require(planned is not None, checker.incomplete_message(key))
                if planned:
                    require(all(planned.get(field) == item.get(field)
                                for field in checker.generated_fields),
                            checker.changed_message(key))
            for key, planned in expected.items():
                if not checker.binds(planned):
                    continue
                obj = planned.get('object') if isinstance(planned.get('object'), dict) else {}
                for message in checker.object_errors(key, obj, current):
                    require(False, message)
                if key not in generated and not checker.allow_manual_bound_objects:
                    require(False, checker.manual_addition_message(key))
                blockers = checker.pass_blockers(key, planned, generated.get(key), current)
                if blockers:
                    gates.setdefault(key, []).extend(blockers)
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            require(False, checker.invalid_message + str(error))
    return gates
