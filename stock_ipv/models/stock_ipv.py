# -*- coding: utf-8 -*-

from odoo import models, fields, api
from odoo.tools.float_utils import float_compare, float_is_zero, float_round
from odoo.exceptions import UserError
from operator import itemgetter


class StockIpv(models.Model):
    _name = 'stock.ipv'
    _description = 'Stock IPV'

    name = fields.Char(required=True)
    @api.model
    def default_get(self, fields):
        res = super().default_get(fields)
        return res

    requested_by = fields.Many2one(
        'res.users', 'Requested by', required=True,
        default=lambda s: s.env.uid,
    )

    location_id = fields.Many2one(
        'stock.location', 'Location', readonly=True,
        domain=[('usage', 'in', ['internal', 'transit'])],
        ondelete="cascade", required=True,
        states={'draft': [('readonly', False)]},
    )

    location_dest_id = fields.Many2one('stock.location',
                                       'Dest Location',
                                       domain=[('usage', 'in', ['internal'])],
                                       ondelete="cascade", required=True,
                                       states={'draft': [('readonly', False)]},
                                       )

    state = fields.Selection([
        ('draft', 'Draft'),
        ('waiting', 'Waiting Another Operation'),
        ('confirmed', 'Waiting'),
        ('assigned', 'Ready'),
        ('done', 'Done'),
        ('cancel', 'Cancelled'),
    ], string='Status', compute='_compute_state',
        copy=False, index=True, readonly=True, store=True, track_visibility='onchange',
        help=" * Draft: not confirmed yet and will not be scheduled until confirmed.\n"
             " * Waiting Another Operation: waiting for another move to proceed before it becomes automatically available (e.g. in Make-To-Order flows).\n"
             " * Waiting: if it is not ready to be sent because the required products could not be reserved.\n"
             " * Ready: products are reserved and ready to be sent. If the shipping policy is 'As soon as possible' this happens as soon as anything is reserved.\n"
             " * Done: has been processed, can't be modified or cancelled anymore.\n"
             " * Cancelled: has been cancelled, can't be confirmed anymore.")

    move_lines = fields.One2many(comodel_name='stock.move',
                               inverse_name='ipv_id',
                               domain=['|', ('package_level_id', '=', False), ('picking_type_entire_packs', '=', False)]
                               )

    picking_ids = fields.One2many('stock.picking',
                                  compute='_compute_picking_ids',
                                  string='Pickings',
                                  readonly=True)

    show_check_availability = fields.Boolean(
        compute='_compute_show_check_availability',
        help='Technical field used to compute whether the check availability button should be shown.')
    show_mark_as_todo = fields.Boolean(
        compute='_compute_show_mark_as_todo',
        help='Technical field used to compute whether the mark as todo button should be shown.')
    show_validate = fields.Boolean(
        compute='_compute_show_validate',
        help='Technical field used to compute whether the validate should be shown.')
    is_locked = fields.Boolean(default=True, help='When the picking is not done this allows changing the '
                                                  'initial demand. When the picking is done this allows '
                                                  'changing the done quantities.')

    date_done = fields.Datetime('Date of Transfer', copy=False, readonly=True,
                                help="Date at which the transfer has been processed or cancelled.")

    @api.depends('move_lines')
    def _compute_picking_ids(self):
        for ipv in self:
            ipv.picking_count = 0
            ipv.picking_ids = self.env['stock.picking']
            ipv.picking_ids = ipv.move_lines.filtered(
                lambda m: m.state != 'cancel').mapped('picking_id')
            ipv.picking_count = len(ipv.picking_ids)

    @api.depends('move_lines.state', 'move_lines.picking_id')
    @api.one
    def _compute_state(self):
        ''' State of a picking depends on the state of its related stock.move
        - Draft: only used for "planned pickings"
        - Waiting: if the picking is not ready to be sent so if
          - (a) no quantity could be reserved at all or if
          - (b) some quantities could be reserved and the shipping policy is "deliver all at once"
        - Waiting another move: if the picking is waiting for another move
        - Ready: if the picking is ready to be sent so if:
          - (a) all quantities are reserved or if
          - (b) some quantities could be reserved and the shipping policy is "as soon as possible"
        - Done: if the picking is done.
        - Cancelled: if the picking is cancelled
        '''
        if not self.move_lines:
            self.state = 'draft'
        elif any(move.state == 'draft' for move in self.move_lines):  # TDE FIXME: should be all ?
            self.state = 'draft'
        elif all(move.state == 'cancel' for move in self.move_lines):
            self.state = 'cancel'
        elif all(move.state in ['cancel', 'done'] for move in self.move_lines):
            self.state = 'done'
        else:
            relevant_move_state = self.move_lines._get_relevant_state_among_moves()
            if relevant_move_state == 'partially_available':
                self.state = 'assigned'
            else:
                self.state = relevant_move_state

    @api.multi
    @api.depends('state', 'move_lines')
    def _compute_show_mark_as_todo(self):
        for ipv in self:
            if ipv.state == 'draft':
                ipv.show_mark_as_todo = True
            elif ipv.state != 'draft' or not ipv.id:
                ipv.show_mark_as_todo = False
            else:
                ipv.show_mark_as_todo = True

    @api.multi
    def _compute_show_check_availability(self):
        for ipv in self:
            has_moves_to_reserve = any(
                move.state in ('waiting', 'confirmed', 'partially_available') and
                float_compare(move.product_uom_qty, 0, precision_rounding=move.product_uom.rounding)
                for move in ipv.move_lines
            )
            ipv.show_check_availability = ipv.is_locked and ipv.state in (
            'confirmed', 'waiting', 'assigned') and has_moves_to_reserve

    @api.multi
    @api.depends('state', 'is_locked')
    def _compute_show_validate(self):
        for ipv in self:
            if ipv.state == 'draft':
                ipv.show_validate = False
            elif ipv.state not in ('draft', 'waiting', 'confirmed', 'assigned') or not ipv.is_locked:
                ipv.show_validate = False
            else:
                ipv.show_validate = True

    @api.model
    def create(self, vals):
        vals['name'] = self.env['ir.sequence'].next_by_code('stock.ipv.seq')

        # On_change es WIP, el create aqui tiene la forma de (0,0,dict)
        if vals.get('move_lines') and vals.get('location_id') and vals.get('location_dest_id'):
            for move in vals['move_lines']:
                if len(move) == 3 and move[0] == 0:
                    move[2]['name'] = vals['name']
                    move[2]['location_id'] = vals['location_id']
                    move[2]['location_dest_id'] = vals['location_dest_id']
        res = super().create(vals)
        return res

    @api.multi
    def action_confirm(self):
        # call `_action_confirm` on every draft move
        self.mapped('move_lines').filtered(lambda move: move.state == 'draft')._action_confirm()
        return True

    @api.multi
    def action_assign(self):
        """ Check availability of picking moves.
        This has the effect of changing the state and reserve quants on available moves, and may
        also impact the state of the picking as it is computed based on move's states.
        @return: True
        """
        self.filtered(lambda picking: picking.state == 'draft').action_confirm()
        moves = self.mapped('move_lines').filtered(lambda move: move.state not in ('draft', 'cancel', 'done'))
        if not moves:
            raise UserError('Nothing to check the availability for.')
        moves._action_assign()
        return True

    @api.multi
    def action_done(self):
        """Changes picking state to done by processing the Stock Moves of the Picking

        Normally that happens when the button "Done" is pressed on a Picking view.
        @return: True
        """
        # TDE FIXME: remove decorator when migration the remaining
        todo_moves = self.mapped('move_lines').filtered(
            lambda move: move.state in ['draft', 'waiting', 'partially_available', 'assigned', 'confirmed'])

        # Check if there are ops not linked to moves yet
        for pick in self:
            move_line_ids = pick.move_lines.mapped(lambda m: m._get_move_lines())

            for ops in move_line_ids.filtered(lambda x: not x.move_id):
                # Search move with this product
                moves = pick.move_lines.filtered(lambda x: x.product_id == ops.product_id)
                moves = sorted(moves, key=lambda m: m.quantity_done < m.product_qty, reverse=True)
                if moves:
                    ops.move_id = moves[0].id
                else:
                    new_move = self.env['stock.move'].create({
                        'name': 'New Move:' + ops.product_id.display_name,
                        'product_id': ops.product_id.id,
                        'product_uom_qty': ops.qty_done,
                        'product_uom': ops.product_uom_id.id,
                        'location_id': pick.location_id.id,
                        'location_dest_id': pick.location_dest_id.id,
                        'picking_id': pick.id,
                        'picking_type_id': pick.picking_type_id.id,
                    })
                    ops.move_id = new_move.id
                    new_move._action_confirm()
                    todo_moves |= new_move
                    # 'qty_done': ops.qty_done})
        todo_moves._action_done()
        self.write({'date_done': fields.Datetime.now()})
        return True

    @api.multi
    def button_validate(self):
        self.ensure_one()
        move_line_ids = self.move_lines.mapped(lambda m: m._get_move_lines())
        if not self.move_lines and not move_line_ids:
            raise UserError('Please add some items to move.')

        precision_digits = self.env['decimal.precision'].precision_get('Product Unit of Measure')
        no_quantities_done = all(float_is_zero(move_line.qty_done, precision_digits=precision_digits) for move_line in
                                 move_line_ids.filtered(lambda m: m.state not in ('done', 'cancel')))
        no_reserved_quantities = all(
            float_is_zero(move_line.product_qty, precision_rounding=move_line.product_uom_id.rounding) for move_line in
            move_line_ids)
        if no_reserved_quantities and no_quantities_done:
            raise UserError(
                'You cannot validate a transfer if no quantites are reserved nor done. To force the transfer, switch in edit more and encode the done quantities.')

        if no_quantities_done:
            for move in self.move_lines:
                for move_line in move.move_line_ids:
                    move_line.qty_done = move_line.product_uom_qty
        self.action_done()
        return


class StockIpvLine(models.Model):
    _name = 'stock.ipv.line'
    _description = 'Ipv Line'

    ipv_id = fields.Many2one('stock.ipv')
    move_id = fields.Many2one('stock.move')





