# EnchantData.StatType 数值对照表

| 值 | 属性 | 备注 |
|---:|---|---|
| 0 | NONE | 空槽（占位） |
| 1 | AttackDamage | 攻击伤害 |
| 2 | AttackSpeed | 攻击速度 |
| 3 | CriticalChance | 暴击率 |
| 4 | CriticalDamage | 暴击伤害 |
| 5 | MaxHp | 最大生命值 |
| 6 | Armor | 护甲 |
| 7 | MovementSpeed | 移动速度 |
| 8 | AreaOfEffect | 范围（AOE） |
| 9 | BaseAttackCountReduction | 基础攻击次数削减 |
| 10 | CooldownReduction | 冷却缩减 |
| 11 | SkillRangeExpansion | 技能范围扩大 |
| 12 | FireResistance | 火焰抗性 |
| 13 | ColdResistance | 冰霜抗性 |
| 14 | LightningResistance | 闪电抗性 |
| 15 | ChaosResistance | 混沌抗性 |
| 16 | DodgeChance | 闪避率 |
| 17 | BlockChance | 格挡率 |
| 18 | MaxDodgeChance | 最大闪避率 |
| 19 | MaxBlockChance | 最大格挡率 |
| 20 | Multistrike | 多重打击 |
| 21 | HpLeech | 生命偷取 |
| 22 | ProjectileCount | 投射物数量 |
| 23 | HpRegenPerSec | 每秒生命回复 |
| 24 | PhysicalDamagePercent | 物理伤害百分比（**+**） |
| 25 | FireDamagePercent | 火焰伤害百分比（**+**） |
| 26 | ColdDamagePercent | 冰霜伤害百分比（**+**） |
| 27 | LightningDamagePercent | 闪电伤害百分比（**+**） |
| 28 | ChaosDamagePercent | 混沌伤害百分比（**+**） |
| 29 | MaxFireResistance | 最大火焰抗性 |
| 30 | MaxColdResistance | 最大冰霜抗性 |
| 31 | MaxLightningResistance | 最大闪电抗性 |
| 32 | MaxChaosResistance | 最大混沌抗性 |
| 33 | AddHpPerHit | 击中回血 |
| 34 | DamageReduction | 伤害减免 |
| 35 | PhysicalDamageReduction | 物理伤害减免 |
| 36 | FireDamageReduction | 火焰伤害减免 |
| 37 | ColdDamageReduction | 冰霜伤害减免 |
| 38 | LightningDamageReduction | 闪电伤害减免 |
| 39 | ChaosDamageReduction | 混沌伤害减免 |
| 40 | DamageAbsorption | 伤害吸收 |
| 41 | DamageAddition | 伤害加成 |
| 42 | PhysicalDamageAddition | 物理伤害加成 |
| 43 | FireDamageAddition | 火焰伤害加成 |
| 44 | ColdDamageAddition | 冰霜伤害加成 |
| 45 | LightningDamageAddition | 闪电伤害加成 |
| 46 | ChaosDamageAddition | 混沌伤害加成 |
| 47 | IncreaseExpAmount | 经验获取提升（百分比） |
| 48 | AdditionalExp | 额外经验（加值） |
| 49 | CastSpeed | 施法速度 |
| 50 | SkillHealIncrease | 技能治疗提升 |
| 51 | SkillDurationIncrease | 技能持续时间提升 |
| 52 | AllElementalResistance | 全元素抗性 |
| 53 | IncreaseProjectileDamage | 投射物伤害提升 |
| 54 | IncreaseMeleeDamage | 近战伤害提升 |
| 55 | IncreaseAreaOfEffectDamage | AOE 伤害提升 |
| 56 | IncreaseSummonDamage | 召唤物伤害提升 |
| 57 | IncreaseProjectileSpeed | 投射物速度提升 |
| 58 | AddHpPerKill | 击杀回血 |
| 59 | AddAllSkillLevel | 全技能等级 +1 |
| 60 | ElementalBlockChance | 元素格挡率 |
| 61 | ElementalDodgeChance | 元素闪避率 |
| 62 | MaxElementalBlockChance | 最大元素格挡率 |
| 63 | MaxElementalDodgeChance | 最大元素闪避率 |

# 真实样本验证
```json
{
    "ItemKey": 910151,
    "UniqueId": 506237948526663179,
    "IsChaotic": false,
    "IsBlocked": false,
    "IsServerPendingItem": false,
    "EnchantCount": [
        0,
        0,
        0
    ],
    "EnchantData": [
        {
            "StatModKey": 102201,
            "Tier": 10,
            "Value": 1,
            "RecipeType": 3,
            "ModType": 0,
            "MaterialKey": 119002,
            "StatType": 22
        },
        {
            "StatModKey": 102001,
            "Tier": 10,
            "Value": 1,
            "RecipeType": 3,
            "ModType": 0,
            "MaterialKey": 119001,
            "StatType": 20
        },
        {
            "StatModKey": 100401,
            "Tier": 10,
            "Value": 168,
            "RecipeType": 4,
            "ModType": 0,
            "MaterialKey": 129002,
            "StatType": 10
        },
        {
            "StatModKey": 100401,
            "Tier": 10,
            "Value": 168,
            "RecipeType": 4,
            "ModType": 0,
            "MaterialKey": 129002,
            "StatType": 10
        },
        {
            "StatModKey": 101601,
            "Tier": 10,
            "Value": 244,
            "RecipeType": 5,
            "ModType": 0,
            "MaterialKey": 139001,
            "StatType": 16
        },
        {
            "StatModKey": 100901,
            "Tier": 10,
            "Value": 302,
            "RecipeType": 5,
            "ModType": 1,
            "MaterialKey": 139001,
            "StatType": 8
        }
    ],
    "DecorationAppliedTotalCount": 0,
    "EngravingAppliedTotalCount": 0,
    "InscriptionAppliedTotalCount": 0
}
```

# 同条 EnchantData 的其他枚举

- **MODTYPE**: 0=FLAT, 1=ADDITIVE, 2=MULTIPLICATIVE
- **ERecipeType**: 0=ALCHEMY, 1=SYNTHESIS, 2=CRAFTING, 3=DECORATION, 4=ENGRAVING, 5=INSCRIPTION, 6=OFFERING, 7=EXTRACTION, 8=NONE
- **ModStatExchangeType**: 0=Raw_Divide1000, 1=Raw_Divide100, 2=Divided
- **StatModKey** 不是枚举，是 `ModData`/`StatMod` 表的外键 ID（本次样本见 100601/100602/102401），需要再反查 `StatModTable` 才能拿到词条名
- **MaterialKey** 是材料 ID（本次样本 110002/110003/110005 都是装饰类材料）
