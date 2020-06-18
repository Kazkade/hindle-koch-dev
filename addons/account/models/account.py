# -*- coding: utf-8 -*-

import time
import math
import re

from odoo.osv import expression
from odoo.tools.float_utils import float_round as round, float_compare
from odoo.tools import DEFAULT_SERVER_DATETIME_FORMAT
from odoo.exceptions import UserError, ValidationError
from odoo import api, fields, models, _, tools
from odoo.tests.common import Form

TYPE_TAX_USE = [
    ('sale', 'Sales'),
    ('purchase', 'Purchases'),
    ('none', 'None'),
]


class AccountAccountType(models.Model):
    _name = "account.account.type"
    _description = "Account Type"

    name = fields.Char(string='Account Type', required=True, translate=True)
    include_initial_balance = fields.Boolean(string="Bring Accounts Balance Forward", help="Used in reports to know if we should consider journal items from the beginning of time instead of from the fiscal year only. Account types that should be reset to zero at each new fiscal year (like expenses, revenue..) should not have this option set.")
    type = fields.Selection([
        ('other', 'Regular'),
        ('receivable', 'Receivable'),
        ('payable', 'Payable'),
        ('liquidity', 'Liquidity'),
    ], required=True, default='other',
        help="The 'Internal Type' is used for features available on "\
        "different types of accounts: liquidity type is for cash or bank accounts"\
        ", payable/receivable is for vendor/customer accounts.")
    internal_group = fields.Selection([
        ('equity', 'Equity'),
        ('asset', 'Asset'),
        ('liability', 'Liability'),
        ('income', 'Income'),
        ('expense', 'Expense'),
        ('off_balance', 'Off Balance'),
    ], string="Internal Group",
        required=True,
        help="The 'Internal Group' is used to filter accounts based on the internal group set on the account type.")
    note = fields.Text(string='Description')


class AccountAccountTag(models.Model):
    _name = 'account.account.tag'
    _description = 'Account Tag'

    name = fields.Char('Tag Name', required=True)
    applicability = fields.Selection([('accounts', 'Accounts'), ('taxes', 'Taxes')], required=True, default='accounts')
    color = fields.Integer('Color Index')
    active = fields.Boolean(default=True, help="Set active to false to hide the Account Tag without removing it.")
    tax_report_line_ids = fields.Many2many(string="Tax Report Lines", comodel_name='account.tax.report.line', relation='account_tax_report_line_tags_rel', help="The tax report lines using this tag")
    tax_negate = fields.Boolean(string="Negate Tax Balance", help="Check this box to negate the absolute value of the balance of the lines associated with this tag in tax report computation.")
    country_id = fields.Many2one(string="Country", comodel_name='res.country', help="Country for which this tag is available, when applied on taxes.")

    @api.constrains('country_id', 'applicability')
    def _validate_tag_country(self):
        for record in self:
            if record.applicability == 'taxes' and not record.country_id:
                raise ValidationError(_("A tag defined to be used on taxes must always have a country set."))


class AccountTaxReportLine(models.Model):
    _name = "account.tax.report.line"
    _description = 'Account Tax Report Line'
    _order = 'sequence'
    _parent_store = True

    name = fields.Char(string="Name", required=True, help="Complete name for this report line, to be used in report.")
    tag_ids = fields.Many2many(string="Tags", comodel_name='account.account.tag', relation='account_tax_report_line_tags_rel', help="Tax tags populating this line")
    country_id = fields.Many2one(string="Country", comodel_name='res.country', required=True, default=lambda x: x.env.company.country_id.id, help="Country for which this line is available.")
    report_action_id = fields.Many2one(string="Report Action", comodel_name='ir.actions.act_window', help="The optional action to call when clicking on this line in accounting reports.")
    children_line_ids = fields.One2many(string="Children Lines", comodel_name='account.tax.report.line', inverse_name='parent_id', help="Lines that should be rendered as children of this one")
    parent_id = fields.Many2one(string="Parent Line", comodel_name='account.tax.report.line')
    sequence = fields.Integer(string='Sequence', required=True,
        help="Sequence determining the order of the lines in the report (smaller ones come first). This order is applied locally per section (so, children of the same line are always rendered one after the other).")
    parent_path = fields.Char(index=True)

    #helper to create tags (positive and negative) on report line creation
    tag_name = fields.Char(string="Tag Name", help="Short name for the tax grid corresponding to this report line. Leave empty if this report line should not correspond to any such grid.")

    #fields used in specific localization reports, where a report line isn't simply the given by the sum of account.move.line with selected tags
    code = fields.Char(string="Code", help="Optional unique code to refer to this line in total formulas")
    formula = fields.Char(string="Formula", help="Python expression used to compute the value of a total line. This field is mutually exclusive with tag_name, setting it turns the line to a total line. Tax report line codes can be used as variables in this expression to refer to the balance of the corresponding lines in the report. A formula cannot refer to another line using a formula.")

    @api.model
    def create(self, vals):
        tag_name = vals.get('tag_name', '')
        if tag_name:
            vals['tag_ids'] = self._get_tags_create_vals(tag_name, vals.get('country_id'))
        return super(AccountTaxReportLine, self).create(vals)

    @api.model
    def _get_tags_create_vals(self, tag_name, country_id):
        minus_tag_vals = {
          'name': '-' + tag_name,
          'applicability': 'taxes',
          'tax_negate': True,
          'country_id': country_id,
        }
        plus_tag_vals = {
          'name': '+' + tag_name,
          'applicability': 'taxes',
          'tax_negate': False,
          'country_id': country_id,
        }
        return [(0, 0, minus_tag_vals), (0, 0, plus_tag_vals)]

    def write(self, vals):
        rslt = super(AccountTaxReportLine, self).write(vals)
        if 'tag_name' in vals:
            # Constraint _validate_tags ensures that tag_ids always contains two
            # tags if not empty: + and -.
            # So, we can impact the whole tag_ids set if tag_name is modified.
            if vals['tag_name']:
                # If some lines in the set did not have tags yet, we create them
                without_tags = self.filtered(lambda x: not x.tag_ids)
                for record in without_tags:
                    record.write({'tag_ids': self._get_tags_create_vals(record.tag_name, record.country_id.id)})

                # We update the name of every tag linked to other lines
                to_update = self - without_tags
                tags_to_update = to_update.mapped('tag_ids')
                minus_child_tags = tags_to_update.filtered(lambda x: x.tax_negate)
                minus_child_tags.write({'name': '-' + vals['tag_name']})

                plus_child_tags = tags_to_update.filtered(lambda x: not x.tax_negate)
                plus_child_tags.write({'name': '+' + vals['tag_name']})

            else:
                # tag_name was set empty, so we remove the tags
                self._delete_tags_from_taxes(self.mapped('tag_ids.id'))
                self.write({'tag_ids': [(2, tag.id, 0) for tag in self.mapped('tag_ids')]})

        if 'country_id' in vals and self.tag_ids:
            # Writing the country of a tax report line should overwrite the country of its tags
            self.tag_ids.write({'country_id': vals['country_id']})

        return rslt

    def unlink(self):
        self._delete_tags_from_taxes(self.mapped('tag_ids.id'))
        children = self.mapped('children_line_ids')
        if children:
            children.unlink()
        return super(AccountTaxReportLine, self).unlink()

    def _delete_tags_from_taxes(self, tag_ids_to_delete):
        """ Based on a list of tag ids, removes them first from the
        repartition lines they are linked to, then deletes them
        from the account move lines.
        """
        if not tag_ids_to_delete:
            # Nothing to do, then!
            return

        self.env.cr.execute("""
            delete from account_account_tag_account_tax_repartition_line_rel
            where account_account_tag_id in %(tag_ids_to_delete)s;
            delete from account_account_tag_account_move_line_rel
            where account_account_tag_id in %(tag_ids_to_delete)s;
        """, {'tag_ids_to_delete': tuple(tag_ids_to_delete)})

        self.env['account.move.line'].invalidate_cache(fnames=['tag_ids'])
        self.env['account.tax.repartition.line'].invalidate_cache(fnames=['tag_ids'])

    @api.constrains('formula', 'tag_name')
    def _validate_formula(self):
        for record in self:
            if record.formula and record.tag_name:
                raise ValidationError(_("Tag name and formula are mutually exclusive, they should not be set together on the same tax report line."))

    @api.constrains('tag_name', 'tag_ids')
    def _validate_tags(self):
        for record in self:
            neg_tags = record.tag_ids.filtered(lambda x: x.tax_negate)
            pos_tags = record.tag_ids.filtered(lambda x: not x.tax_negate)
            if record.tag_ids and (len(neg_tags) !=1 or len(pos_tags) != 1):
                raise ValidationError(_("If tags are defined for a tax report line, only two are allowed on it: a positive and a negative one."))

    @api.constrains('tag_name', 'country_id')
    def _validate_tag_name_unicity(self):
        for record in self:
            if record.tag_name:
                other_lines_with_same_tag = self.env['account.tax.report.line'].search_count([('tag_name', '=', record.tag_name), ('id', '!=', record.id), ('country_id', '=', record.country_id.id)])
                if other_lines_with_same_tag:
                    raise ValidationError(_("Tag name %(tag)s is used by more than one tax report line in %(country)s. Each tag name should only be used once per country.") % {'tag': record.tag_name, 'country': record.country_id.name})

    @api.onchange('parent_id')
    def _onchange_parent_id(self):
        """ If the parent of a report line is changed, the sequence of this line
        must be updated, in order for it to match  the sequence numbering of its
        parent. By default, we add it as the last line of this section.
        """
        if self.country_id:
            lines_domain = [('country_id', '=', self.country_id.id)]
            if self.parent_id:
                lines_domain.append(('parent_id', '=', self.parent_id.id))

            last_line = self.env['account.tax.report.line'].search(lines_domain, order="sequence desc", limit=1)
            self.sequence = last_line.sequence + 1

#----------------------------------------------------------
# Accounts
#----------------------------------------------------------


class AccountAccount(models.Model):
    _name = "account.account"
    _description = "Account"
    _order = "code"

    @api.constrains('internal_type', 'reconcile')
    def _check_reconcile(self):
        for account in self:
            if account.internal_type in ('receivable', 'payable') and account.reconcile == False:
                raise ValidationError(_('You cannot have a receivable/payable account that is not reconcilable. (account code: %s)') % account.code)

    @api.constrains('user_type_id')
    def _check_user_type_id(self):
        data_unaffected_earnings = self.env.ref('account.data_unaffected_earnings')
        result = self.read_group([('user_type_id', '=', data_unaffected_earnings.id)], ['company_id'], ['company_id'])
        for res in result:
            if res.get('company_id_count', 0) >= 2:
                account_unaffected_earnings = self.search([('company_id', '=', res['company_id'][0]),
                                                           ('user_type_id', '=', data_unaffected_earnings.id)])
                raise ValidationError(_('You cannot have more than one account with "Current Year Earnings" as type. (accounts: %s)') % [a.code for a in account_unaffected_earnings])

    name = fields.Char(required=True, index=True)
    currency_id = fields.Many2one('res.currency', string='Account Currency',
        help="Forces all moves for this account to have this account currency.")
    code = fields.Char(size=64, required=True, index=True)
    deprecated = fields.Boolean(index=True, default=False)
    used = fields.Boolean(store=False, search='_search_used')
    user_type_id = fields.Many2one('account.account.type', string='Type', required=True,
        help="Account Type is used for information purpose, to generate country-specific legal reports, and set the rules to close a fiscal year and generate opening entries.")
    internal_type = fields.Selection(related='user_type_id.type', string="Internal Type", store=True, readonly=True)
    internal_group = fields.Selection(related='user_type_id.internal_group', string="Internal Group", store=True, readonly=True)
    #has_unreconciled_entries = fields.Boolean(compute='_compute_has_unreconciled_entries',
    #    help="The account has at least one unreconciled debit and credit since last time the invoices & payments matching was performed.")
    reconcile = fields.Boolean(string='Allow Reconciliation', default=False,
        help="Check this box if this account allows invoices & payments matching of journal items.")
    tax_ids = fields.Many2many('account.tax', 'account_account_tax_default_rel',
        'account_id', 'tax_id', string='Default Taxes', context={'append_type_to_tax_name': True})
    note = fields.Text('Internal Notes')
    company_id = fields.Many2one('res.company', string='Company', required=True,
        default=lambda self: self.env.company)
    tag_ids = fields.Many2many('account.account.tag', 'account_account_account_tag', string='Tags', help="Optional tags you may want to assign for custom reporting")
    group_id = fields.Many2one('account.group')
    root_id = fields.Many2one('account.root', compute='_compute_account_root', store=True)

    opening_debit = fields.Monetary(string="Opening debit", compute='_compute_opening_debit_credit', inverse='_set_opening_debit', help="Opening debit value for this account.")
    opening_credit = fields.Monetary(string="Opening credit", compute='_compute_opening_debit_credit', inverse='_set_opening_credit', help="Opening credit value for this account.")

    _sql_constraints = [
        ('code_company_uniq', 'unique (code,company_id)', 'The code of the account must be unique per company !')
    ]

    @api.constrains('reconcile', 'internal_group', 'tax_ids')
    def _constrains_reconcile(self):
        for record in self:
            if record.internal_group == 'off_balance':
                if record.reconcile:
                    raise UserError(_('An Off-Balance account can not be reconcilable'))
                if record.tax_ids:
                    raise UserError(_('An Off-Balance account can not have taxes'))

    @api.constrains('company_id')
    def _check_company_consistency(self):
        if not self:
            return

        self.flush(['company_id'])
        self._cr.execute('''
            SELECT line.id
            FROM account_move_line line
            JOIN account_account account ON account.id = line.account_id
            WHERE line.account_id IN %s
            AND line.company_id != account.company_id
        ''', [tuple(self.ids)])
        if self._cr.fetchone():
            raise UserError(_("You can't change the company of your account since there are some journal items linked to it."))

    @api.constrains('user_type_id')
    def _check_user_type_id(self):
        if not self:
            return

        self.flush(['user_type_id'])
        self._cr.execute('''
            SELECT account.id
            FROM account_account account
            JOIN account_account_type acc_type ON account.user_type_id = acc_type.id
            JOIN account_journal journal ON journal.default_credit_account_id = account.id OR journal.default_debit_account_id = account.id
            WHERE account.id IN %s
            AND acc_type.type IN ('receivable', 'payable')
            AND journal.type IN ('sale', 'purchase')
            LIMIT 1;
        ''', [tuple(self.ids)])

        if self._cr.fetchone():
            raise ValidationError(_("The account is already in use in a 'sale' or 'purchase' journal. This means that the account's type couldn't be 'receivable' or 'payable'."))

    @api.depends('code')
    def _compute_account_root(self):
        # this computes the first 2 digits of the account.
        # This field should have been a char, but the aim is to use it in a side panel view with hierarchy, and it's only supported by many2one fields so far.
        # So instead, we make it a many2one to a psql view with what we need as records.
        for record in self:
            record.root_id = (ord(record.code[0]) * 1000 + ord(record.code[1:2] or '\x00')) if record.code else False

    def _search_used(self, operator, value):
        if operator not in ['=', '!='] or not isinstance(value, bool):
            raise UserError(_('Operation not supported'))
        if operator != '=':
            value = not value
        self._cr.execute("""
            SELECT id FROM account_account account
            WHERE EXISTS (SELECT * FROM account_move_line aml WHERE aml.account_id = account.id LIMIT 1)
        """)
        return [('id', 'in' if value else 'not in', [r[0] for r in self._cr.fetchall()])]

    @api.model
    def _search_new_account_code(self, company, digits, prefix):
        for num in range(1, 10000):
            new_code = str(prefix.ljust(digits - 1, '0')) + str(num)
            rec = self.search([('code', '=', new_code), ('company_id', '=', company.id)], limit=1)
            if not rec:
                return new_code
        raise UserError(_('Cannot generate an unused account code.'))

    def _compute_opening_debit_credit(self):
        for record in self:
            opening_debit = opening_credit = 0.0
            if record.company_id.account_opening_move_id:
                for line in self.env['account.move.line'].search([('account_id', '=', record.id),
                                                                 ('move_id','=', record.company_id.account_opening_move_id.id)]):
                    if line.debit:
                        opening_debit += line.debit
                    elif line.credit:
                        opening_credit += line.credit
            record.opening_debit = opening_debit
            record.opening_credit = opening_credit

    def _set_opening_debit(self):
        self._set_opening_debit_credit(self.opening_debit, 'debit')

    def _set_opening_credit(self):
        self._set_opening_debit_credit(self.opening_credit, 'credit')

    def _set_opening_debit_credit(self, amount, field):
        """ Generic function called by both opening_debit and opening_credit's
        inverse function. 'Amount' parameter is the value to be set, and field
        either 'debit' or 'credit', depending on which one of these two fields
        got assigned.
        """
        self.company_id.create_op_move_if_non_existant()
        opening_move = self.company_id.account_opening_move_id

        if opening_move.state == 'draft':
            # check whether we should create a new move line or modify an existing one
            account_op_lines = self.env['account.move.line'].search([('account_id', '=', self.id),
                                                                      ('move_id','=', opening_move.id),
                                                                      (field,'!=', False),
                                                                      (field,'!=', 0.0)]) # 0.0 condition important for import

            if account_op_lines:
                op_aml_debit = sum(account_op_lines.mapped('debit'))
                op_aml_credit = sum(account_op_lines.mapped('credit'))

                # There might be more than one line on this account if the opening entry was manually edited
                # If so, we need to merge all those lines into one before modifying its balance
                opening_move_line = account_op_lines[0]
                if len(account_op_lines) > 1:
                    merge_write_cmd = [(1, opening_move_line.id, {'debit': op_aml_debit, 'credit': op_aml_credit, 'partner_id': None ,'name': _("Opening balance")})]
                    unlink_write_cmd = [(2, line.id) for line in account_op_lines[1:]]
                    opening_move.write({'line_ids': merge_write_cmd + unlink_write_cmd})

                if amount:
                    # modify the line
                    opening_move_line.with_context(check_move_validity=False)[field] = amount
                else:
                    # delete the line (no need to keep a line with value = 0)
                    opening_move_line.with_context(check_move_validity=False).unlink()

            elif amount:
                # create a new line, as none existed before
                self.env['account.move.line'].with_context(check_move_validity=False).create({
                        'name': _('Opening balance'),
                        field: amount,
                        'move_id': opening_move.id,
                        'account_id': self.id,
                })

            # Then, we automatically balance the opening move, to make sure it stays valid
            if not 'import_file' in self.env.context:
                # When importing a file, avoid recomputing the opening move for each account and do it at the end, for better performances
                self.company_id._auto_balance_opening_move()

    @api.model
    def default_get(self, default_fields):
        """If we're creating a new account through a many2one, there are chances that we typed the account code
        instead of its name. In that case, switch both fields values.
        """
        default_name = self._context.get('default_name')
        default_code = self._context.get('default_code')
        if default_name and not default_code:
            try:
                default_code = int(default_name)
            except ValueError:
                pass
            if default_code:
                default_name = False
        contextual_self = self.with_context(default_name=default_name, default_code=default_code)
        return super(AccountAccount, contextual_self).default_get(default_fields)

    @api.model
    def _name_search(self, name, args=None, operator='ilike', limit=100, name_get_uid=None):
        args = args or []
        domain = []
        if name:
            domain = ['|', ('code', '=ilike', name.split(' ')[0] + '%'), ('name', operator, name)]
            if operator in expression.NEGATIVE_TERM_OPERATORS:
                domain = ['&', '!'] + domain[1:]
        account_ids = self._search(expression.AND([domain, args]), limit=limit, access_rights_uid=name_get_uid)
        return models.lazy_name_get(self.browse(account_ids).with_user(name_get_uid))

    @api.onchange('user_type_id')
    def _onchange_user_type_id(self):
        self.reconcile = self.internal_type in ('receivable', 'payable')
        if self.internal_type == 'liquidity':
            self.reconcile = False
        elif self.internal_group == 'off_balance':
            self.reconcile = False
            self.tax_ids = False
        elif self.internal_group == 'income' and not self.tax_ids:
            self.tax_ids = self.company_id.account_sale_tax_id
        elif self.internal_group == 'expense' and not self.tax_ids:
            self.tax_ids = self.company_id.account_purchase_tax_id

    @api.onchange('code')
    def onchange_code(self):
        AccountGroup = self.env['account.group']

        group = False
        code_prefix = self.code

        # find group with longest matching prefix
        while code_prefix:
            matching_group = AccountGroup.search([('code_prefix', '=', code_prefix)], limit=1)
            if matching_group:
                group = matching_group
                break
            code_prefix = code_prefix[:-1]
        self.group_id = group

    def name_get(self):
        result = []
        for account in self:
            name = account.code + ' ' + account.name
            result.append((account.id, name))
        return result

    @api.returns('self', lambda value: value.id)
    def copy(self, default=None):
        default = dict(default or {})
        if default.get('code', False):
            return super(AccountAccount, self).copy(default)
        try:
            default['code'] = (str(int(self.code) + 10) or '').zfill(len(self.code))
            default.setdefault('name', _("%s (copy)") % (self.name or ''))
            while self.env['account.account'].search([('code', '=', default['code']),
                                                      ('company_id', '=', default.get('company_id', False) or self.company_id.id)], limit=1):
                default['code'] = (str(int(default['code']) + 10) or '')
                default['name'] = _("%s (copy)") % (self.name or '')
        except ValueError:
            default['code'] = _("%s (copy)") % (self.code or '')
            default['name'] = self.name
        return super(AccountAccount, self).copy(default)

    @api.model
    def load(self, fields, data):
        """ Overridden for better performances when importing a list of account
        with opening debit/credit. In that case, the auto-balance is postpone
        until the whole file has been imported.
        """
        rslt = super(AccountAccount, self).load(fields, data)

        if 'import_file' in self.env.context:
            companies = self.search([('id', 'in', rslt['ids'])]).mapped('company_id')
            for company in companies:
                company._auto_balance_opening_move()
        return rslt

    def _toggle_reconcile_to_true(self):
        '''Toggle the `reconcile´ boolean from False -> True

        Note that: lines with debit = credit = amount_currency = 0 are set to `reconciled´ = True
        '''
        if not self.ids:
            return None
        query = """
            UPDATE account_move_line SET
                reconciled = CASE WHEN debit = 0 AND credit = 0 AND amount_currency = 0
                    THEN true ELSE false END,
                amount_residual = (debit-credit),
                amount_residual_currency = amount_currency
            WHERE full_reconcile_id IS NULL and account_id IN %s
        """
        self.env.cr.execute(query, [tuple(self.ids)])

    def _toggle_reconcile_to_false(self):
        '''Toggle the `reconcile´ boolean from True -> False

        Note that it is disallowed if some lines are partially reconciled.
        '''
        if not self.ids:
            return None
        partial_lines_count = self.env['account.move.line'].search_count([
            ('account_id', 'in', self.ids),
            ('full_reconcile_id', '=', False),
            ('|'),
            ('matched_debit_ids', '!=', False),
            ('matched_credit_ids', '!=', False),
        ])
        if partial_lines_count > 0:
            raise UserError(_('You cannot switch an account to prevent the reconciliation '
                              'if some partial reconciliations are still pending.'))
        query = """
            UPDATE account_move_line
                SET amount_residual = 0, amount_residual_currency = 0
            WHERE full_reconcile_id IS NULL AND account_id IN %s
        """
        self.env.cr.execute(query, [tuple(self.ids)])

    def write(self, vals):
        # Do not allow changing the company_id when account_move_line already exist
        if vals.get('company_id', False):
            move_lines = self.env['account.move.line'].search([('account_id', 'in', self.ids)], limit=1)
            for account in self:
                if (account.company_id.id != vals['company_id']) and move_lines:
                    raise UserError(_('You cannot change the owner company of an account that already contains journal items.'))
        if 'reconcile' in vals:
            if vals['reconcile']:
                self.filtered(lambda r: not r.reconcile)._toggle_reconcile_to_true()
            else:
                self.filtered(lambda r: r.reconcile)._toggle_reconcile_to_false()

        if vals.get('currency_id'):
            for account in self:
                if self.env['account.move.line'].search_count([('account_id', '=', account.id), ('currency_id', 'not in', (False, vals['currency_id']))]):
                    raise UserError(_('You cannot set a currency on this account as it already has some journal entries having a different foreign currency.'))

        return super(AccountAccount, self).write(vals)

    def unlink(self):
        if self.env['account.move.line'].search([('account_id', 'in', self.ids)], limit=1):
            raise UserError(_('You cannot perform this action on an account that contains journal items.'))
        #Checking whether the account is set as a property to any Partner or not
        values = ['account.account,%s' % (account_id,) for account_id in self.ids]
        partner_prop_acc = self.env['ir.property'].search([('value_reference', 'in', values)], limit=1)
        if partner_prop_acc:
            raise UserError(_('You cannot remove/deactivate an account which is set on a customer or vendor.'))
        return super(AccountAccount, self).unlink()

    def action_open_reconcile(self):
        self.ensure_one()
        # Open reconciliation view for this account
        action_context = {'show_mode_selector': False, 'mode': 'accounts', 'account_ids': [self.id,]}
        return {
            'type': 'ir.actions.client',
            'tag': 'manual_reconciliation_view',
            'context': action_context,
        }

    def action_read_account(self):
        self.ensure_one()
        return {
            'name': self.display_name,
            'type': 'ir.actions.act_window',
            'view_type': 'form',
            'view_mode': 'form',
            'res_model': 'account.account',
            'res_id': self.id,
            'target': 'new',
        }

    def action_duplicate_accounts(self):
        for account in self.browse(self.env.context['active_ids']):
            account.copy()


class AccountGroup(models.Model):
    _name = "account.group"
    _description = 'Account Group'
    _parent_store = True
    _order = 'code_prefix'

    parent_id = fields.Many2one('account.group', index=True, ondelete='cascade')
    parent_path = fields.Char(index=True)
    name = fields.Char(required=True)
    code_prefix = fields.Char()

    def name_get(self):
        result = []
        for group in self:
            name = group.name
            if group.code_prefix:
                name = group.code_prefix + ' ' + name
            result.append((group.id, name))
        return result

    @api.model
    def _name_search(self, name, args=None, operator='ilike', limit=100, name_get_uid=None):
        args = args or []
        if operator == 'ilike' and not (name or '').strip():
            domain = []
        else:
            criteria_operator = ['|'] if operator not in expression.NEGATIVE_TERM_OPERATORS else ['&', '!']
            domain = criteria_operator + [('code_prefix', '=ilike', name + '%'), ('name', operator, name)]
        group_ids = self._search(expression.AND([domain, args]), limit=limit, access_rights_uid=name_get_uid)
        return models.lazy_name_get(self.browse(group_ids).with_user(name_get_uid))


class AccountRoot(models.Model):
    _name = 'account.root'
    _description = 'Account codes first 2 digits'
    _auto = False

    name = fields.Char()
    parent_id = fields.Many2one('account.root')
    company_id = fields.Many2one('res.company')

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute('''
            CREATE OR REPLACE VIEW %s AS (
            SELECT DISTINCT ASCII(code) * 1000 + ASCII(SUBSTRING(code,2,1)) AS id,
                   LEFT(code,2) AS name,
                   ASCII(code) AS parent_id,
                   company_id
            FROM account_account WHERE code IS NOT NULL
            UNION ALL
            SELECT DISTINCT ASCII(code) AS id,
                   LEFT(code,1) AS name,
                   NULL::int AS parent_id,
                   company_id
            FROM account_account WHERE code IS NOT NULL
            )''' % (self._table,)
        )


class AccountJournalGroup(models.Model):
    _name = 'account.journal.group'
    _description = "Account Journal Group"

    name = fields.Char("Journal Group", required=True, translate=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    excluded_journal_ids = fields.Many2many('account.journal', string="Excluded Journals", domain="[('company_id', '=', company_id)]")
    sequence = fields.Integer(default=10)


class AccountJournal(models.Model):
    _name = "account.journal"
    _description = "Journal"
    _order = 'sequence, type, code'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    def _default_inbound_payment_methods(self):
        return self.env.ref('account.account_payment_method_manual_in')

    def _default_outbound_payment_methods(self):
        return self.env.ref('account.account_payment_method_manual_out')

    def __get_bank_statements_available_sources(self):
        return [('undefined', _('Undefined Yet'))]

    def _get_bank_statements_available_sources(self):
        return self.__get_bank_statements_available_sources()

    name = fields.Char(string='Journal Name', required=True)
    code = fields.Char(string='Short Code', size=5, required=True, help="The journal entries of this journal will be named using this prefix.")
    active = fields.Boolean(default=True, help="Set active to false to hide the Journal without removing it.")
    type = fields.Selection([
            ('sale', 'Sales'),
            ('purchase', 'Purchase'),
            ('cash', 'Cash'),
            ('bank', 'Bank'),
            ('general', 'Miscellaneous'),
        ], required=True,
        help="Select 'Sale' for customer invoices journals.\n"\
        "Select 'Purchase' for vendor bills journals.\n"\
        "Select 'Cash' or 'Bank' for journals that are used in customer or vendor payments.\n"\
        "Select 'General' for miscellaneous operations journals.")
    type_control_ids = fields.Many2many('account.account.type', 'account_journal_type_rel', 'journal_id', 'type_id', string='Account Types Allowed')
    account_control_ids = fields.Many2many('account.account', 'account_account_type_rel', 'journal_id', 'account_id', string='Accounts Allowed',
        domain="[('deprecated', '=', False), ('company_id', '=', company_id)]")
    default_credit_account_id = fields.Many2one('account.account', string='Default Credit Account',
        domain=[('deprecated', '=', False)], help="It acts as a default account for credit amount",
        ondelete='restrict')
    default_debit_account_id = fields.Many2one('account.account', string='Default Debit Account',
        domain="[('deprecated', '=', False), ('company_id', '=', company_id)]", help="It acts as a default account for debit amount", ondelete='restrict')
    restrict_mode_hash_table = fields.Boolean(string="Lock Posted Entries with Hash",
        help="If ticked, the accounting entry or invoice receives a hash as soon as it is posted and cannot be modified anymore.")
    sequence_id = fields.Many2one('ir.sequence', string='Entry Sequence',
        help="This field contains the information related to the numbering of the journal entries of this journal.", required=True, copy=False)
    refund_sequence_id = fields.Many2one('ir.sequence', string='Credit Note Entry Sequence',
        help="This field contains the information related to the numbering of the credit note entries of this journal.", copy=False)
    sequence = fields.Integer(help='Used to order Journals in the dashboard view', default=10)
    sequence_number_next = fields.Integer(string='Next Number',
        help='The next sequence number will be used for the next invoice.',
        compute='_compute_seq_number_next',
        inverse='_inverse_seq_number_next')
    refund_sequence_number_next = fields.Integer(string='Credit Notes Next Number',
        help='The next sequence number will be used for the next credit note.',
        compute='_compute_refund_seq_number_next',
        inverse='_inverse_refund_seq_number_next')

    invoice_reference_type = fields.Selection(string='Communication Type', required=True, selection=[('none', 'Free'), ('partner', 'Based on Customer'), ('invoice', 'Based on Invoice')], default='invoice', help='You can set here the default communication that will appear on customer invoices, once validated, to help the customer to refer to that particular invoice when making the payment.')
    invoice_reference_model = fields.Selection(string='Communication Standard', required=True, selection=[('odoo', 'Odoo'),('euro', 'European')], default='odoo', help="You can choose different models for each type of reference. The default one is the Odoo reference.")

    #groups_id = fields.Many2many('res.groups', 'account_journal_group_rel', 'journal_id', 'group_id', string='Groups')
    currency_id = fields.Many2one('res.currency', help='The currency used to enter statement', string="Currency")
    company_id = fields.Many2one('res.company', string='Company', required=True, index=True, default=lambda self: self.env.company,
        help="Company related to this journal")

    refund_sequence = fields.Boolean(string='Dedicated Credit Note Sequence', help="Check this box if you don't want to share the same sequence for invoices and credit notes made from this journal", default=False)

    inbound_payment_method_ids = fields.Many2many('account.payment.method', 'account_journal_inbound_payment_method_rel', 'journal_id', 'inbound_payment_method',
        domain=[('payment_type', '=', 'inbound')], string='For Incoming Payments', default=lambda self: self._default_inbound_payment_methods(),
        help="Manual: Get paid by cash, check or any other method outside of Odoo.\n"\
             "Electronic: Get paid automatically through a payment acquirer by requesting a transaction on a card saved by the customer when buying or subscribing online (payment token).\n"\
             "Batch Deposit: Encase several customer checks at once by generating a batch deposit to submit to your bank. When encoding the bank statement in Odoo,you are suggested to reconcile the transaction with the batch deposit. Enable this option from the settings.")
    outbound_payment_method_ids = fields.Many2many('account.payment.method', 'account_journal_outbound_payment_method_rel', 'journal_id', 'outbound_payment_method',
        domain=[('payment_type', '=', 'outbound')], string='For Outgoing Payments', default=lambda self: self._default_outbound_payment_methods(),
        help="Manual:Pay bill by cash or any other method outside of Odoo.\n"\
             "Check:Pay bill by check and print it from Odoo.\n"\
             "SEPA Credit Transfer: Pay bill from a SEPA Credit Transfer file you submit to your bank. Enable this option from the settings.")
    at_least_one_inbound = fields.Boolean(compute='_methods_compute', store=True)
    at_least_one_outbound = fields.Boolean(compute='_methods_compute', store=True)
    profit_account_id = fields.Many2one('account.account', domain="[('company_id', '=', company_id)]", string='Profit Account', help="Used to register a profit when the ending balance of a cash register differs from what the system computes")
    loss_account_id = fields.Many2one('account.account', domain="[('company_id', '=', company_id)]", string='Loss Account', help="Used to register a loss when the ending balance of a cash register differs from what the system computes")

    # Bank journals fields
    company_partner_id = fields.Many2one('res.partner', related='company_id.partner_id', string='Account Holder', readonly=True, store=False)
    bank_account_id = fields.Many2one('res.partner.bank', string="Bank Account", ondelete='restrict', copy=False, domain="[('partner_id','=', company_partner_id), '|', ('company_id', '=', False), ('company_id', '=', company_id)]")
    bank_statements_source = fields.Selection(selection=_get_bank_statements_available_sources, string='Bank Feeds', default='undefined', help="Defines how the bank statements will be registered")
    bank_acc_number = fields.Char(related='bank_account_id.acc_number', readonly=False)
    bank_id = fields.Many2one('res.bank', related='bank_account_id.bank_id', readonly=False)
    post_at = fields.Selection([('pay_val', 'Payment Validation'), ('bank_rec', 'Bank Reconciliation')], string="Post At", default='pay_val')

    # alias configuration for journals
    alias_id = fields.Many2one('mail.alias', string='Alias', copy=False)
    alias_domain = fields.Char('Alias domain', compute='_compute_alias_domain', default=lambda self: self.env["ir.config_parameter"].sudo().get_param("mail.catchall.domain"))
    alias_name = fields.Char('Alias Name', related='alias_id.alias_name', help="It creates draft invoices and bills by sending an email.", readonly=False)

    journal_group_ids = fields.Many2many('account.journal.group', domain="[('company_id', '=', company_id)]", string="Journal Groups")

    secure_sequence_id = fields.Many2one('ir.sequence', help='Sequence to use to ensure the securisation of data', readonly=True, copy=False)

    _sql_constraints = [
        ('code_company_uniq', 'unique (code, name, company_id)', 'The code and name of the journal must be unique per company !'),
    ]

    def _compute_alias_domain(self):
        alias_domain = self.env["ir.config_parameter"].sudo().get_param("mail.catchall.domain")
        for record in self:
            record.alias_domain = alias_domain

    # do not depend on 'sequence_id.date_range_ids', because
    # sequence_id._get_current_sequence() may invalidate it!
    @api.depends('sequence_id.use_date_range', 'sequence_id.number_next_actual')
    def _compute_seq_number_next(self):
        '''Compute 'sequence_number_next' according to the current sequence in use,
        an ir.sequence or an ir.sequence.date_range.
        '''
        for journal in self:
            if journal.sequence_id:
                sequence = journal.sequence_id._get_current_sequence()
                journal.sequence_number_next = sequence.number_next_actual
            else:
                journal.sequence_number_next = 1

    def _inverse_seq_number_next(self):
        '''Inverse 'sequence_number_next' to edit the current sequence next number.
        '''
        for journal in self:
            if journal.sequence_id and journal.sequence_number_next:
                sequence = journal.sequence_id._get_current_sequence()
                sequence.sudo().number_next = journal.sequence_number_next

    # do not depend on 'refund_sequence_id.date_range_ids', because
    # refund_sequence_id._get_current_sequence() may invalidate it!
    @api.depends('refund_sequence_id.use_date_range', 'refund_sequence_id.number_next_actual')
    def _compute_refund_seq_number_next(self):
        '''Compute 'sequence_number_next' according to the current sequence in use,
        an ir.sequence or an ir.sequence.date_range.
        '''
        for journal in self:
            if journal.refund_sequence_id and journal.refund_sequence:
                sequence = journal.refund_sequence_id._get_current_sequence()
                journal.refund_sequence_number_next = sequence.number_next_actual
            else:
                journal.refund_sequence_number_next = 1

    def _inverse_refund_seq_number_next(self):
        '''Inverse 'refund_sequence_number_next' to edit the current sequence next number.
        '''
        for journal in self:
            if journal.refund_sequence_id and journal.refund_sequence and journal.refund_sequence_number_next:
                sequence = journal.refund_sequence_id._get_current_sequence()
                sequence.sudo().number_next = journal.refund_sequence_number_next

    @api.constrains('currency_id', 'default_credit_account_id', 'default_debit_account_id')
    def _check_currency(self):
        for journal in self:
            if journal.currency_id:
                if journal.default_credit_account_id and not journal.default_credit_account_id.currency_id.id == journal.currency_id.id:
                    raise ValidationError(_('The currency of the journal should be the same than the default credit account.'))
                if journal.default_debit_account_id and not journal.default_debit_account_id.currency_id.id == journal.currency_id.id:
                    raise ValidationError(_('The currency of the journal should be the same than the default debit account.'))

    @api.constrains('type', 'bank_account_id')
    def _check_bank_account(self):
        for journal in self:
            if journal.type == 'bank' and journal.bank_account_id:
                if journal.bank_account_id.company_id != journal.company_id:
                    raise ValidationError(_('The bank account of a bank journal must belong to the same company (%s).') % journal.company_id.name)
                # A bank account can belong to a customer/supplier, in which case their partner_id is the customer/supplier.
                # Or they are part of a bank journal and their partner_id must be the company's partner_id.
                if journal.bank_account_id.partner_id != journal.company_id.partner_id:
                    raise ValidationError(_('The holder of a journal\'s bank account must be the company (%s).') % journal.company_id.name)

    @api.constrains('company_id')
    def _check_company_consistency(self):
        if not self:
            return

        self.flush(['company_id'])
        self._cr.execute('''
            SELECT move.id
            FROM account_move move
            JOIN account_journal journal ON journal.id = move.journal_id
            WHERE move.journal_id IN %s
            AND move.company_id != journal.company_id
        ''', [tuple(self.ids)])
        if self._cr.fetchone():
            raise UserError(_("You can't change the company of your journal since there are some journal entries linked to it."))

    @api.constrains('type', 'default_credit_account_id', 'default_debit_account_id')
    def _check_type_default_credit_account_id_type(self):
        journals_to_check = self.filtered(lambda journal: journal.type in ('sale', 'purchase'))
        accounts_to_check = journals_to_check.mapped('default_debit_account_id') + journals_to_check.mapped('default_credit_account_id')
        if any(account.user_type_id.type in ('receivable', 'payable') for account in accounts_to_check):
            raise ValidationError(_("The type of the journal's default credit/debit account shouldn't be 'receivable' or 'payable'."))

    @api.onchange('default_debit_account_id')
    def onchange_debit_account_id(self):
        if not self.default_credit_account_id:
            self.default_credit_account_id = self.default_debit_account_id

    @api.onchange('default_credit_account_id')
    def onchange_credit_account_id(self):
        if not self.default_debit_account_id:
            self.default_debit_account_id = self.default_credit_account_id

    @api.onchange('type')
    def _onchange_type(self):
        self.refund_sequence = self.type in ('sale', 'purchase')

    def _get_alias_values(self, type, alias_name=None):
        if not alias_name:
            alias_name = self.name
            if self.company_id != self.env.ref('base.main_company'):
                alias_name += '-' + str(self.company_id.name)
        return {
            'alias_defaults': {'type': type == 'purchase' and 'in_invoice' or 'out_invoice', 'company_id': self.company_id.id, 'journal_id': self.id},
            'alias_parent_thread_id': self.id,
            'alias_name': alias_name,
        }

    def unlink(self):
        bank_accounts = self.env['res.partner.bank'].browse()
        for bank_account in self.mapped('bank_account_id'):
            accounts = self.search([('bank_account_id', '=', bank_account.id)])
            if accounts <= self:
                bank_accounts += bank_account
        self.mapped('alias_id').unlink()
        ret = super(AccountJournal, self).unlink()
        bank_accounts.unlink()
        return ret

    @api.returns('self', lambda value: value.id)
    def copy(self, default=None):
        default = dict(default or {})
        default.update(
            code=_("%s (copy)") % (self.code or ''),
            name=_("%s (copy)") % (self.name or ''))
        return super(AccountJournal, self).copy(default)

    def _update_mail_alias(self, vals):
        self.ensure_one()
        alias_values = self._get_alias_values(type=vals.get('type') or self.type, alias_name=vals.get('alias_name'))
        if self.alias_id:
            self.alias_id.write(alias_values)
        else:
            self.alias_id = self.env['mail.alias'].with_context(alias_model_name='account.move',
                alias_parent_model_name='account.journal').create(alias_values)

        if vals.get('alias_name'):
            # remove alias_name to avoid useless write on alias
            del(vals['alias_name'])

    def write(self, vals):
        for journal in self:
            company = journal.company_id
            if ('company_id' in vals and journal.company_id.id != vals['company_id']):
                if self.env['account.move'].search([('journal_id', '=', journal.id)], limit=1):
                    raise UserError(_('This journal already contains items, therefore you cannot modify its company.'))
                company = self.env['res.company'].browse(vals['company_id'])
                if journal.bank_account_id.company_id and journal.bank_account_id.company_id != company:
                    journal.bank_account_id.write({
                        'company_id': company.id,
                        'partner_id': company.partner_id.id,
                    })
            if ('code' in vals and journal.code != vals['code']):
                if self.env['account.move'].search([('journal_id', 'in', self.ids)], limit=1):
                    raise UserError(_('This journal already contains items, therefore you cannot modify its short name.'))
                new_prefix = self._get_sequence_prefix(vals['code'], refund=False)
                journal.sequence_id.write({'prefix': new_prefix})
                if journal.refund_sequence_id:
                    new_prefix = self._get_sequence_prefix(vals['code'], refund=True)
                    journal.refund_sequence_id.write({'prefix': new_prefix})
            if 'currency_id' in vals:
                if not 'default_debit_account_id' in vals and journal.default_debit_account_id:
                    journal.default_debit_account_id.currency_id = vals['currency_id']
                if not 'default_credit_account_id' in vals and journal.default_credit_account_id:
                    journal.default_credit_account_id.currency_id = vals['currency_id']
                if journal.bank_account_id:
                    journal.bank_account_id.currency_id = vals['currency_id']
            if 'bank_account_id' in vals:
                if not vals.get('bank_account_id'):
                    raise UserError(_('You cannot remove the bank account from the journal once set.'))
                else:
                    bank_account = self.env['res.partner.bank'].browse(vals['bank_account_id'])
                    if bank_account.partner_id != company.partner_id:
                        raise UserError(_("The partners of the journal's company and the related bank account mismatch."))
            if 'alias_name' in vals:
                journal._update_mail_alias(vals)
            if 'restrict_mode_hash_table' in vals and not vals.get('restrict_mode_hash_table'):
                journal_entry = self.env['account.move'].search([('journal_id', '=', self.id), ('state', '=', 'posted'), ('secure_sequence_number', '!=', 0)], limit=1)
                if len(journal_entry) > 0:
                    field_string = self._fields['restrict_mode_hash_table'].get_description(self.env)['string']
                    raise UserError(_("You cannot modify the field %s of a journal that already has accounting entries.") % field_string)
        result = super(AccountJournal, self).write(vals)

        # Create the bank_account_id if necessary
        if 'bank_acc_number' in vals:
            for journal in self.filtered(lambda r: r.type == 'bank' and not r.bank_account_id):
                journal.set_bank_account(vals.get('bank_acc_number'), vals.get('bank_id'))
        # create the relevant refund sequence
        if vals.get('refund_sequence'):
            for journal in self.filtered(lambda j: j.type in ('sale', 'purchase') and not j.refund_sequence_id):
                journal_vals = {
                    'name': journal.name,
                    'company_id': journal.company_id.id,
                    'code': journal.code,
                    'refund_sequence_number_next': vals.get('refund_sequence_number_next', journal.refund_sequence_number_next),
                }
                journal.refund_sequence_id = self.sudo()._create_sequence(journal_vals, refund=True).id
        # Changing the 'post_at' option will post the draft payment moves and change the related invoices' state.
        if 'post_at' in vals and vals['post_at'] != 'bank_rec':
            draft_moves = self.env['account.move'].search([('journal_id', 'in', self.ids), ('state', '=', 'draft')])
            pending_payments = draft_moves.mapped('line_ids.payment_id')
            pending_payments.mapped('move_line_ids.move_id').post()
            pending_payments.mapped('reconciled_invoice_ids').filtered(lambda x: x.invoice_payment_state == 'in_payment').write({'invoice_payment_state': 'paid'})
        for record in self:
            if record.restrict_mode_hash_table and not record.secure_sequence_id:
                record._create_secure_sequence(['secure_sequence_id'])

        return result

    @api.model
    def _get_sequence_prefix(self, code, refund=False):
        prefix = code.upper()
        if refund:
            prefix = 'R' + prefix
        return prefix + '/%(range_year)s/'

    @api.model
    def _create_sequence(self, vals, refund=False):
        """ Create new no_gap entry sequence for every new Journal"""
        prefix = self._get_sequence_prefix(vals['code'], refund)
        seq_name = refund and vals['code'] + _(': Refund') or vals['code']
        seq = {
            'name': _('%s Sequence') % seq_name,
            'implementation': 'no_gap',
            'prefix': prefix,
            'padding': 4,
            'number_increment': 1,
            'use_date_range': True,
        }
        if 'company_id' in vals:
            seq['company_id'] = vals['company_id']
        seq = self.env['ir.sequence'].create(seq)
        seq_date_range = seq._get_current_sequence()
        seq_date_range.number_next = refund and vals.get('refund_sequence_number_next', 1) or vals.get('sequence_number_next', 1)
        return seq

    @api.model
    def _prepare_liquidity_account(self, name, company, currency_id, type):
        '''
        This function prepares the value to use for the creation of the default debit and credit accounts of a
        liquidity journal (created through the wizard of generating COA from templates for example).

        :param name: name of the bank account
        :param company: company for which the wizard is running
        :param currency_id: ID of the currency in which is the bank account
        :param type: either 'cash' or 'bank'
        :return: mapping of field names and values
        :rtype: dict
        '''
        digits = 6
        acc = self.env['account.account'].search([('company_id', '=', company.id)], limit=1)
        if acc:
            digits = len(acc.code)
        # Seek the next available number for the account code
        if type == 'bank':
            account_code_prefix = company.bank_account_code_prefix or ''
        else:
            account_code_prefix = company.cash_account_code_prefix or company.bank_account_code_prefix or ''

        liquidity_type = self.env.ref('account.data_account_type_liquidity')
        return {
                'name': name,
                'currency_id': currency_id or False,
                'code': self.env['account.account']._search_new_account_code(company, digits, account_code_prefix),
                'user_type_id': liquidity_type and liquidity_type.id or False,
                'company_id': company.id,
        }

    @api.model
    def get_next_bank_cash_default_code(self, journal_type, company_id):
        journal_code_base = (journal_type == 'cash' and 'CSH' or 'BNK')
        journals = self.env['account.journal'].search([('code', 'like', journal_code_base + '%'), ('company_id', '=', company_id)])
        for num in range(1, 100):
            # journal_code has a maximal size of 5, hence we can enforce the boundary num < 100
            journal_code = journal_code_base + str(num)
            if journal_code not in journals.mapped('code'):
                return journal_code

    @api.model
    def create(self, vals):
        company_id = vals.get('company_id', self.env.company.id)
        if vals.get('type') in ('bank', 'cash'):
            # For convenience, the name can be inferred from account number
            if not vals.get('name') and 'bank_acc_number' in vals:
                vals['name'] = vals['bank_acc_number']

            # If no code provided, loop to find next available journal code
            if not vals.get('code'):
                vals['code'] = self.get_next_bank_cash_default_code(vals['type'], company_id)
                if not vals['code']:
                    raise UserError(_("Cannot generate an unused journal code. Please fill the 'Shortcode' field."))


            # Create a default debit/credit account if not given
            default_account = vals.get('default_debit_account_id') or vals.get('default_credit_account_id')
            company = self.env['res.company'].browse(company_id)
            if not default_account:
                account_vals = self._prepare_liquidity_account(vals.get('name'), company, vals.get('currency_id'), vals.get('type'))
                default_account = self.env['account.account'].create(account_vals)
                vals['default_debit_account_id'] = default_account.id
                vals['default_credit_account_id'] = default_account.id
            if vals['type'] == 'cash':
                if not vals.get('profit_account_id'):
                    vals['profit_account_id'] = company.default_cash_difference_income_account_id.id
                if not vals.get('loss_account_id'):
                    vals['loss_account_id'] = company.default_cash_difference_expense_account_id.id

        if 'refund_sequence' not in vals:
            vals['refund_sequence'] = vals['type'] in ('sale', 'purchase')

        # We just need to create the relevant sequences according to the chosen options
        if not vals.get('sequence_id'):
            vals.update({'sequence_id': self.sudo()._create_sequence(vals).id})
        if vals.get('type') in ('sale', 'purchase') and vals.get('refund_sequence') and not vals.get('refund_sequence_id'):
            vals.update({'refund_sequence_id': self.sudo()._create_sequence(vals, refund=True).id})
        journal = super(AccountJournal, self.with_context(mail_create_nolog=True)).create(vals)
        if 'alias_name' in vals:
            journal._update_mail_alias(vals)

        # Create the bank_account_id if necessary
        if journal.type == 'bank' and not journal.bank_account_id and vals.get('bank_acc_number'):
            journal.set_bank_account(vals.get('bank_acc_number'), vals.get('bank_id'))

        return journal

    def set_bank_account(self, acc_number, bank_id=None):
        """ Create a res.partner.bank and set it as value of the  field bank_account_id """
        self.ensure_one()
        self.bank_account_id = self.env['res.partner.bank'].create({
            'acc_number': acc_number,
            'bank_id': bank_id,
            'company_id': self.company_id.id,
            'currency_id': self.currency_id.id,
            'partner_id': self.company_id.partner_id.id,
        }).id

    def name_get(self):
        res = []
        for journal in self:
            currency = journal.currency_id or journal.company_id.currency_id
            name = "%s (%s)" % (journal.name, currency.name)
            res += [(journal.id, name)]
        return res

    @api.model
    def _name_search(self, name, args=None, operator='ilike', limit=100, name_get_uid=None):
        args = args or []

        if operator == 'ilike' and not (name or '').strip():
            domain = []
        else:
            connector = '&' if operator in expression.NEGATIVE_TERM_OPERATORS else '|'
            domain = [connector, ('code', operator, name), ('name', operator, name)]
        journal_ids = self._search(expression.AND([domain, args]), limit=limit, access_rights_uid=name_get_uid)
        return models.lazy_name_get(self.browse(journal_ids).with_user(name_get_uid))

    @api.depends('inbound_payment_method_ids', 'outbound_payment_method_ids')
    def _methods_compute(self):
        for journal in self:
            journal.at_least_one_inbound = bool(len(journal.inbound_payment_method_ids))
            journal.at_least_one_outbound = bool(len(journal.outbound_payment_method_ids))

    def action_configure_bank_journal(self):
        """ This function is called by the "configure" button of bank journals,
        visible on dashboard if no bank statement source has been defined yet
        """
        # We simply call the setup bar function.
        return self.env['res.company'].setting_init_bank_account_action()

    def create_invoice_from_attachment(self, attachment_ids=[]):
        ''' Create the invoices from files.
         :return: A action redirecting to account.move tree/form view.
        '''
        attachments = self.env['ir.attachment'].browse(attachment_ids)
        if not attachments:
            raise UserError(_("No attachment was provided"))

        invoices = self.env['account.move']
        for attachment in attachments:
            invoice = self.env['account.move'].create({})
            attachment.write({'res_model': 'mail.compose.message'})
            invoice.message_post(attachment_ids=[attachment.id])
            invoices += invoice

        action_vals = {
            'name': _('Generated Documents'),
            'domain': [('id', 'in', invoices.ids)],
            'res_model': 'account.move',
            'views': [[False, "tree"], [False, "form"]],
            'type': 'ir.actions.act_window',
            'context': self._context
        }
        if len(invoices) == 1:
            action_vals.update({'res_id': invoices[0].id, 'view_mode': 'form'})
        else:
            action_vals['view_mode'] = 'tree,form'
        return action_vals

    def _create_secure_sequence(self, sequence_fields):
        """This function creates a no_gap sequence on each journal in self that will ensure
        a unique number is given to all posted account.move in such a way that we can always
        find the previous move of a journal entry on a specific journal.
        """
        for journal in self:
            vals_write = {}
            for seq_field in sequence_fields:
                if not journal[seq_field]:
                    vals = {
                        'name': _('Securisation of %s - %s') % (seq_field, journal.name),
                        'code': 'SECUR%s-%s' % (journal.id, seq_field),
                        'implementation': 'no_gap',
                        'prefix': '',
                        'suffix': '',
                        'padding': 0,
                        'company_id': journal.company_id.id}
                    seq = self.env['ir.sequence'].create(vals)
                    vals_write[seq_field] = seq.id
            if vals_write:
                journal.write(vals_write)


class ResPartnerBank(models.Model):
    _inherit = "res.partner.bank"

    journal_id = fields.One2many('account.journal', 'bank_account_id', domain=[('type', '=', 'bank')], string='Account Journal', readonly=True,
        help="The accounting journal corresponding to this bank account.")

    @api.constrains('journal_id')
    def _check_journal_id(self):
        for bank in self:
            if len(bank.journal_id) > 1:
                raise ValidationError(_('A bank account can belong to only one journal.'))


#----------------------------------------------------------
# Tax
#----------------------------------------------------------

class AccountTaxGroup(models.Model):
    _name = 'account.tax.group'
    _description = 'Tax Group'
    _ord